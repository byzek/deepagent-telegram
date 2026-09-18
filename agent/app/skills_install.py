"""Fetch & install external skills into the per-user `/skills/` store.

A "skill" here is just a Markdown playbook. This module pulls SKILL.md-style
skills from a git repo / URL and writes them straight into the user's `skills`
namespace, so the agent can install a whole library on command without the
`execute`+`cat`+`write_file` juggling across the sandbox and virtual FS.

Supported `source` forms:
  * owner/repo                          (GitHub, default branch)
  * owner/repo/sub/path                 (only skills under that subpath)
  * https://github.com/owner/repo
  * https://github.com/owner/repo/tree/<ref>/<subpath>
  * https://github.com/owner/repo/blob/<ref>/<path>/SKILL.md   (single file)
  * https://raw.githubusercontent.com/.../SKILL.md             (single file)

The parsing/selection helpers are pure (stdlib only) so they're unit-testable
without the LangChain/deepagents stack. HTTP is injected for the same reason.
"""
from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

_SHORTHAND = re.compile(r"^[\w.-]+/[\w.-]+(?:/.+)?$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_CAP = 25          # max skills installed per call
_FILES_PER_SKILL = 50      # max files materialized per skill folder
_MAX_FILE_BYTES = 1_000_000  # skip individual files larger than this
_SANDBOX_SKILLS_ROOT = "/workspace/skills"


@dataclass(frozen=True)
class SourceSpec:
    kind: str  # "github_repo" | "raw"
    owner: str = ""
    repo: str = ""
    ref: str | None = None
    subpath: str = ""
    url: str = ""


def _raw_github_url(owner: str, repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def parse_source(source: str) -> SourceSpec:
    """Classify a user-supplied skill source. Raises ValueError on junk."""
    s = (source or "").strip()
    if not s:
        raise ValueError("empty source")

    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        parts = [p for p in u.path.split("/") if p]
        if u.netloc == "github.com":
            if len(parts) < 2:
                raise ValueError(f"not a GitHub repo URL: {source!r}")
            owner, repo = parts[0], parts[1]
            if len(parts) >= 4 and parts[2] in ("tree", "blob"):
                ref = parts[3]
                sub = "/".join(parts[4:])
                if parts[2] == "blob" and sub.endswith(".md"):
                    return SourceSpec("raw", url=_raw_github_url(owner, repo, ref, sub))
                return SourceSpec("github_repo", owner=owner, repo=repo, ref=ref, subpath=sub)
            return SourceSpec("github_repo", owner=owner, repo=repo,
                              subpath="/".join(parts[2:]))
        if s.endswith(".md"):
            return SourceSpec("raw", url=s)
        raise ValueError(
            f"unsupported URL {source!r}: give a GitHub repo, a tree/blob URL, "
            "or a direct link to a .md skill file"
        )

    if _SHORTHAND.match(s):
        parts = s.split("/")
        return SourceSpec("github_repo", owner=parts[0], repo=parts[1],
                          subpath="/".join(parts[2:]))

    raise ValueError(
        f"unrecognized skill source {source!r}: use owner/repo, a GitHub URL, "
        "or a direct .md link"
    )


def skill_key_from_path(path: str, fallback: str) -> str:
    """Derive a safe `<name>.md` store key from a repo file path.

    `airtable/SKILL.md` -> `airtable.md`; a root `SKILL.md` -> `<fallback>.md`;
    `guides/deploy.md` -> `deploy.md`.
    """
    parts = [p for p in path.split("/") if p and p != ".."]
    fname = parts[-1] if parts else ""
    if fname.lower() == "skill.md":
        base = parts[-2] if len(parts) >= 2 else fallback
    elif fname.lower().endswith(".md"):
        base = fname[:-3]
    else:
        base = fname or fallback
    base = _SAFE.sub("-", base).strip("-._") or fallback
    return f"{base}.md"


def select_skill_paths(tree_paths: list[str], subpath: str) -> list[str]:
    """Pick skill files from a repo tree, scoped to `subpath`.

    Prefers `SKILL.md` files; if none exist under the scope, falls back to all
    `*.md` files there.
    """
    sub = subpath.strip("/")
    if sub:
        prefix = sub + "/"
        scoped = [p for p in tree_paths if p == sub or p.startswith(prefix)]
    else:
        scoped = list(tree_paths)

    skill_md = [p for p in scoped if p.rsplit("/", 1)[-1].lower() == "skill.md"]
    if skill_md:
        return sorted(skill_md)
    return sorted(p for p in scoped if p.lower().endswith(".md"))


def skill_dirs_from_tree(
    tree_paths: list[str], subpath: str, repo: str
) -> list[tuple[str, str]]:
    """Find SKILL.md-defined skills in a repo tree, scoped to `subpath`.

    Returns sorted (name, dir) pairs, where `dir` is the folder containing the
    SKILL.md ("" for a repo-root SKILL.md) and `name` is the derived skill name.
    """
    sub = subpath.strip("/")
    out: list[tuple[str, str]] = []
    for path in tree_paths:
        if path.rsplit("/", 1)[-1].lower() != "skill.md":
            continue
        d = path[: -len("SKILL.md")].strip("/")  # parent dir of the SKILL.md
        if sub and not (d == sub or d.startswith(sub + "/")):
            continue
        base = d.rsplit("/", 1)[-1] if d else repo
        name = _SAFE.sub("-", base).strip("-._") or repo
        out.append((name, d))
    return sorted(set(out))


def files_under(tree_paths: list[str], dirprefix: str) -> list[str]:
    """Return paths under `dirprefix`, relative to it ("" == whole tree)."""
    d = dirprefix.strip("/")
    if not d:
        return sorted(tree_paths)
    prefix = d + "/"
    return sorted(p[len(prefix):] for p in tree_paths if p.startswith(prefix))


def _safe_rel(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and ".." not in parts and not rel.startswith("/")


def sandbox_write_command(path: str, content: str) -> str:
    """Shell command that writes `content` to `path` in the sandbox, creating
    parent dirs. Content is base64-encoded so arbitrary text is shell-safe."""
    blob = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent = path.rsplit("/", 1)[0]
    return (
        f"mkdir -p {shlex.quote(parent)} && "
        f"printf %s {shlex.quote(blob)} | base64 -d > {shlex.quote(path)}"
    )


def _with_location_header(content: str, sandbox_path: str) -> str:
    return (
        f"> Installed skill. Bundled code lives in the sandbox at `{sandbox_path}` "
        f"— run it with the `execute` tool.\n\n{content}"
    )


async def _default_fetch_text(url: str, token: str = "") -> str:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.text


async def _default_fetch_json(url: str, token: str = "") -> dict:
    import httpx

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


async def _default_run_cmd(command: str, sandbox_url: str, token: str) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{sandbox_url}/execute",
            json={"command": command},
            headers={"X-Sandbox-Token": token},
        )
        r.raise_for_status()
        return r.json()


async def install_skills(
    store,
    user_id: str,
    source: str,
    *,
    fetch_text=None,
    fetch_json=None,
    run_cmd=None,
    token: str = "",
    sandbox_url: str = "",
    sandbox_token: str = "",
    cap: int = _DEFAULT_CAP,
) -> list[dict]:
    """Fetch skill(s) from `source` and install them for ("skills", user_id).

    A SKILL.md-defined skill installs its playbook into the store (for semantic
    recall) AND materializes its whole folder — scripts included — into the
    sandbox under /workspace/skills/<name>/ so `execute` can run it. Loose *.md
    guides (no SKILL.md) are store-only. Installing overwrites (== updates).

    Returns a list of {"key", "path", "bytes", "files", "sandbox_path"} entries.
    """
    if fetch_text is None:
        fetch_text = lambda u: _default_fetch_text(u, token)  # noqa: E731
    if fetch_json is None:
        fetch_json = lambda u: _default_fetch_json(u, token)  # noqa: E731
    if run_cmd is None:
        run_cmd = lambda c: _default_run_cmd(c, sandbox_url, sandbox_token)  # noqa: E731

    spec = parse_source(source)
    namespace = ("skills", user_id)

    if spec.kind == "raw":
        content = await fetch_text(spec.url)
        key = skill_key_from_path(urlparse(spec.url).path, "skill")
        await store.aput(namespace, key, {"content": content})
        return [{"key": key, "path": spec.url, "bytes": len(content),
                 "files": 0, "sandbox_path": None}]

    ref = spec.ref
    if not ref:
        meta = await fetch_json(f"https://api.github.com/repos/{spec.owner}/{spec.repo}")
        ref = meta.get("default_branch", "main")

    tree = await fetch_json(
        f"https://api.github.com/repos/{spec.owner}/{spec.repo}"
        f"/git/trees/{ref}?recursive=1"
    )
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]

    def raw(path: str) -> str:
        return _raw_github_url(spec.owner, spec.repo, ref, path)

    skill_dirs = skill_dirs_from_tree(paths, spec.subpath, spec.repo)
    installed: list[dict] = []

    if skill_dirs:
        for name, d in skill_dirs[:cap]:
            rels = files_under(paths, d)
            md_rel = next(
                (r for r in rels if r.rsplit("/", 1)[-1].lower() == "skill.md"), None
            )
            if md_rel is None:
                continue
            md_content = await fetch_text(raw(f"{d}/{md_rel}" if d else md_rel))
            sandbox_path = f"{_SANDBOX_SKILLS_ROOT}/{name}/"

            n_files = 0
            for rel in rels[:_FILES_PER_SKILL]:
                if not _safe_rel(rel):
                    continue
                content = md_content if rel == md_rel else await fetch_text(
                    raw(f"{d}/{rel}" if d else rel)
                )
                if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                    continue
                await run_cmd(sandbox_write_command(f"{sandbox_path}{rel}", content))
                n_files += 1

            await store.aput(
                namespace, f"{name}.md",
                {"content": _with_location_header(md_content, sandbox_path)},
            )
            installed.append({
                "key": f"{name}.md", "path": d or ".", "bytes": len(md_content),
                "files": n_files, "sandbox_path": sandbox_path,
            })
        return installed

    # Fallback: a repo of loose *.md playbooks with no SKILL.md — store only.
    for path in select_skill_paths(paths, spec.subpath)[:cap]:
        content = await fetch_text(raw(path))
        key = skill_key_from_path(path, spec.repo)
        await store.aput(namespace, key, {"content": content})
        installed.append({"key": key, "path": path, "bytes": len(content),
                          "files": 0, "sandbox_path": None})
    return installed
