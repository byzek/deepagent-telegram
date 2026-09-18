"""Unit tests for external skill installation (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_skills_install.py   (from agent/)
Run with pytest:      pytest tests/test_skills_install.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.skills_install import (  # noqa: E402
    files_under,
    install_skills,
    parse_source,
    sandbox_write_command,
    select_skill_paths,
    skill_dirs_from_tree,
    skill_key_from_path,
)


class FakeStore:
    """Minimal async store double matching the LangGraph put interface."""

    def __init__(self):
        self.puts: dict[tuple, dict] = {}

    async def aput(self, namespace, key, value):
        self.puts[(namespace, key)] = value


def test_parse_source_shorthand():
    spec = parse_source("owner/repo")
    assert spec.kind == "github_repo"
    assert (spec.owner, spec.repo, spec.ref, spec.subpath) == ("owner", "repo", None, "")


def test_parse_source_shorthand_with_subpath():
    spec = parse_source("owner/repo/skills/airtable")
    assert spec.kind == "github_repo"
    assert spec.subpath == "skills/airtable"


def test_parse_source_repo_url():
    spec = parse_source("https://github.com/owner/repo")
    assert spec.kind == "github_repo"
    assert (spec.owner, spec.repo) == ("owner", "repo")


def test_parse_source_tree_url_with_ref_and_subpath():
    spec = parse_source("https://github.com/owner/repo/tree/main/skills/airtable")
    assert spec.kind == "github_repo"
    assert spec.ref == "main"
    assert spec.subpath == "skills/airtable"


def test_parse_source_blob_md_becomes_raw():
    spec = parse_source("https://github.com/owner/repo/blob/main/skills/airtable/SKILL.md")
    assert spec.kind == "raw"
    assert spec.url == (
        "https://raw.githubusercontent.com/owner/repo/main/skills/airtable/SKILL.md"
    )


def test_parse_source_raw_md_url():
    url = "https://raw.githubusercontent.com/o/r/main/x/SKILL.md"
    spec = parse_source(url)
    assert spec.kind == "raw"
    assert spec.url == url


def test_parse_source_rejects_junk():
    for bad in ["", "   ", "not a url", "ftp://x/y", "https://example.com/page"]:
        try:
            parse_source(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_skill_key_from_path():
    assert skill_key_from_path("airtable/SKILL.md", "repo") == "airtable.md"
    assert skill_key_from_path("skills/web-search/SKILL.md", "repo") == "web-search.md"
    assert skill_key_from_path("SKILL.md", "myrepo") == "myrepo.md"
    assert skill_key_from_path("guides/deploy.md", "repo") == "deploy.md"
    # unsafe characters collapse to a safe filename
    assert skill_key_from_path("we ird/../SKILL.md", "repo") == "we-ird.md"


def test_select_skill_paths_prefers_skill_md():
    paths = [
        "README.md",
        "airtable/SKILL.md",
        "airtable/reference.md",
        "notion/SKILL.md",
    ]
    assert select_skill_paths(paths, "") == ["airtable/SKILL.md", "notion/SKILL.md"]


def test_select_skill_paths_falls_back_to_md():
    paths = ["docs/a.md", "docs/b.md", "src/x.py"]
    assert select_skill_paths(paths, "docs") == ["docs/a.md", "docs/b.md"]


def test_select_skill_paths_scopes_to_subpath():
    paths = ["a/SKILL.md", "b/SKILL.md"]
    assert select_skill_paths(paths, "b") == ["b/SKILL.md"]


def test_skill_dirs_from_tree():
    paths = [
        "README.md",
        "airtable/SKILL.md",
        "airtable/client.py",
        "notion/SKILL.md",
    ]
    assert skill_dirs_from_tree(paths, "", "repo") == [
        ("airtable", "airtable"),
        ("notion", "notion"),
    ]


def test_skill_dirs_root_uses_fallback_name():
    assert skill_dirs_from_tree(["SKILL.md", "run.py"], "", "myrepo") == [("myrepo", "")]


def test_skill_dirs_scoped_to_subpath():
    paths = ["skills/airtable/SKILL.md", "skills/notion/SKILL.md"]
    assert skill_dirs_from_tree(paths, "skills/airtable", "repo") == [
        ("airtable", "skills/airtable"),
    ]


def test_files_under():
    paths = ["airtable/SKILL.md", "airtable/client.py", "notion/SKILL.md"]
    assert files_under(paths, "airtable") == ["SKILL.md", "client.py"]
    # root dir -> full relative paths
    assert files_under(["SKILL.md", "run.py"], "") == ["SKILL.md", "run.py"]


def test_sandbox_write_command_roundtrips_content():
    cmd = sandbox_write_command("/workspace/skills/x/run.py", "print('hi')\n")
    assert "mkdir -p" in cmd
    assert "base64 -d" in cmd
    # the base64 blob must decode back to the exact content
    import base64
    import re
    blob = re.search(r"printf %s (\S+) \| base64 -d", cmd).group(1)
    assert base64.b64decode(blob).decode() == "print('hi')\n"


def _run(coro):
    return asyncio.run(coro)


def test_install_skills_from_repo():
    store = FakeStore()

    async def fetch_json(url):
        if url.endswith("/repos/owner/repo"):
            return {"default_branch": "main"}
        if "git/trees/main" in url:
            return {"tree": [
                {"path": "airtable/SKILL.md", "type": "blob"},
                {"path": "notion/SKILL.md", "type": "blob"},
                {"path": "README.md", "type": "blob"},
            ]}
        raise AssertionError(f"unexpected json url {url}")

    async def fetch_text(url):
        return f"# content of {url.rsplit('/', 2)[-2]}"

    async def run_cmd(command):
        return {"exit_code": 0}

    installed = _run(install_skills(
        store, "telegram:1", "owner/repo",
        fetch_text=fetch_text, fetch_json=fetch_json, run_cmd=run_cmd,
    ))

    keys = sorted(i["key"] for i in installed)
    assert keys == ["airtable.md", "notion.md"]
    assert "# content of airtable" in store.puts[
        (("skills", "telegram:1"), "airtable.md")]["content"]


def test_install_skills_materializes_code_to_sandbox():
    store = FakeStore()
    ran: list[str] = []

    async def fetch_json(url):
        if url.endswith("/repos/owner/repo"):
            return {"default_branch": "main"}
        return {"tree": [
            {"path": "airtable/SKILL.md", "type": "blob"},
            {"path": "airtable/client.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]}

    async def fetch_text(url):
        if url.endswith("client.py"):
            return "print('run')\n"
        return "# Airtable skill\n"

    async def run_cmd(command):
        ran.append(command)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    installed = _run(install_skills(
        store, "telegram:1", "owner/repo",
        fetch_text=fetch_text, fetch_json=fetch_json, run_cmd=run_cmd,
    ))

    assert len(installed) == 1
    entry = installed[0]
    assert entry["key"] == "airtable.md"
    assert entry["files"] == 2
    assert entry["sandbox_path"] == "/workspace/skills/airtable/"
    # both files were written into the sandbox
    assert any("/workspace/skills/airtable/client.py" in c for c in ran)
    assert any("/workspace/skills/airtable/SKILL.md" in c for c in ran)
    # stored playbook records where its code lives
    stored = store.puts[(("skills", "telegram:1"), "airtable.md")]["content"]
    assert "/workspace/skills/airtable/" in stored
    assert "# Airtable skill" in stored


def test_install_skills_loose_md_stays_store_only():
    """A repo of loose *.md guides (no SKILL.md) is store-only, never sandboxed."""
    store = FakeStore()

    async def fetch_json(url):
        if url.endswith("/repos/o/r"):
            return {"default_branch": "main"}
        return {"tree": [{"path": "guides/deploy.md", "type": "blob"}]}

    async def fetch_text(url):
        return "# deploy guide"

    async def run_cmd(command):
        raise AssertionError("loose markdown must not touch the sandbox")

    installed = _run(install_skills(
        store, "u", "o/r",
        fetch_text=fetch_text, fetch_json=fetch_json, run_cmd=run_cmd,
    ))
    assert [i["key"] for i in installed] == ["deploy.md"]
    assert installed[0]["files"] == 0


def test_install_skills_single_raw():
    store = FakeStore()

    async def fetch_text(url):
        return "# hello skill"

    installed = _run(install_skills(
        store, "telegram:1",
        "https://raw.githubusercontent.com/o/r/main/airtable/SKILL.md",
        fetch_text=fetch_text, fetch_json=None,
    ))
    assert len(installed) == 1
    assert installed[0]["key"] == "airtable.md"
    assert store.puts[(("skills", "telegram:1"), "airtable.md")] == {"content": "# hello skill"}


def test_install_skills_respects_cap():
    store = FakeStore()

    async def fetch_json(url):
        if url.endswith("/repos/o/r"):
            return {"default_branch": "main"}
        return {"tree": [
            {"path": f"s{i}/SKILL.md", "type": "blob"} for i in range(50)
        ]}

    async def fetch_text(url):
        return "x"

    async def run_cmd(command):
        return {"exit_code": 0}

    installed = _run(install_skills(
        store, "u", "o/r", fetch_text=fetch_text, fetch_json=fetch_json,
        run_cmd=run_cmd, cap=10,
    ))
    assert len(installed) == 10


def test_install_skills_none_found_returns_empty():
    store = FakeStore()

    async def fetch_json(url):
        if url.endswith("/repos/o/r"):
            return {"default_branch": "main"}
        return {"tree": [{"path": "src/x.py", "type": "blob"}]}

    async def fetch_text(url):
        raise AssertionError("should not fetch text when nothing selected")

    installed = _run(install_skills(
        store, "u", "o/r", fetch_text=fetch_text, fetch_json=fetch_json,
    ))
    assert installed == []


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
