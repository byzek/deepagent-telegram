"""Export a user's skills (playbooks + sandbox code) as a seed bundle.

Used to stand up a fresh instance that keeps every skill but none of the
memory: the tar written to stdout unpacks straight into another checkout's
`agent/seed/` (skills/*.md + workspace/...). On boot, `seed_users` installs the
playbooks for each allowlisted user and `seed_workspace` restores the code into
the sandbox — both never overwrite existing files.

    python -m app.skills_bundle telegram:12345 > bundle.tar
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import shlex
import sys
import tarfile
from pathlib import Path

from app.catalog import list_tools_command
from app.sandbox_fs import upload_command

log = logging.getLogger(__name__)

SEED_WORKSPACE = Path(__file__).resolve().parent.parent / "seed" / "workspace"


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


async def export_bundle(store, user_id: str, run_cmd, out) -> None:
    with tarfile.open(fileobj=out, mode="w") as tar:
        for item in await store.asearch(("skills", user_id), limit=1000):
            _add(tar, f"skills/{item.key}", (item.value or {}).get("content", "").encode())
        res = await run_cmd(list_tools_command())
        for path in (res.get("stdout") or "").splitlines():
            if not path.strip():
                continue
            got = await run_cmd(f"base64 {shlex.quote(path)}")
            _add(tar, "workspace/" + path.removeprefix("/workspace/"),
                 base64.b64decode(got.get("stdout") or ""))


async def seed_workspace(root: Path, run_cmd) -> None:
    """Copy seed/workspace/** into the sandbox's /workspace, skipping files
    that already exist (so the agent's later edits survive restarts)."""
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        dest = "/workspace/" + p.relative_to(root).as_posix()
        await run_cmd(f"[ -e {shlex.quote(dest)} ] || {{ {upload_command(dest, p.read_bytes())}; }}")
        log.info("Seeded sandbox file %s", dest)


async def _amain(user_id: str) -> None:
    from app.config import settings
    from app.embeddings import build_embeddings
    from app.memory import build_memory_store
    from app.persistence import build_persistence
    from app.skills_install import _default_run_cmd

    persistence = await build_persistence(settings.database_url)
    mem = await build_memory_store(persistence.pool, build_embeddings())
    try:
        await export_bundle(
            mem.store, user_id,
            lambda c: _default_run_cmd(c, settings.sandbox_url, settings.sandbox_token),
            sys.stdout.buffer,
        )
    finally:
        if mem.aclose is not None:
            await mem.aclose()
        await persistence.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.skills_bundle <platform:user_id> > bundle.tar")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(_amain(sys.argv[1]))
