"""Seed starter skills & memories into the store for each allowed user.

Idempotent: an existing file is never overwritten, so users keep their edits
across restarts. Seed content lives in the image under ./seed/{memories,skills}.
User ids are already platform-qualified (e.g. "telegram:12345").
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_SEED_ROOT = Path(__file__).resolve().parent.parent / "seed"


async def _seed_dir(store, namespace_prefix: str, user_id: str, src: Path) -> None:
    if not src.is_dir():
        return
    namespace = (namespace_prefix, user_id)
    for path in sorted(src.glob("*")):
        if not path.is_file():
            continue
        existing = await store.aget(namespace, path.name)
        if existing is not None:
            continue
        await store.aput(namespace, path.name, {"content": path.read_text()})
        log.info("Seeded %s/%s for user %s", namespace_prefix, path.name, user_id)


async def seed_users(store, user_ids) -> None:
    for user_id in user_ids:
        await _seed_dir(store, "memories", user_id, _SEED_ROOT / "memories")
        await _seed_dir(store, "skills", user_id, _SEED_ROOT / "skills")
