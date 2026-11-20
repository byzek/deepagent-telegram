"""Snapshot of the agent's tools and skills for the dashboard.

Splits everything into what shipped with the image ("baked") and what the agent
picked up while running ("learned"/"developed"). The agent writes a JSON
snapshot into Postgres periodically, so the dashboard stays read-only and works
with every memory backend (the qdrant store isn't in Postgres).

The classification helpers are pure (stdlib only) so they're unit-testable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
from pathlib import Path

log = logging.getLogger(__name__)

SEED_SKILLS = Path(__file__).resolve().parent.parent / "seed" / "skills"
# Sandbox locations where runnable agent-made/installed code lives.
TOOL_ROOTS = ("/workspace/skills", "/workspace/tools")
SCRIPT_EXTS = (".py", ".sh", ".js", ".ts", ".rb", ".pl", ".bash")

DDL = """
CREATE TABLE IF NOT EXISTS agent_catalog (
    id    INTEGER PRIMARY KEY,
    ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
    data  JSONB NOT NULL
);
"""


def classify_skills(stored: dict[str, str], seed: dict[str, str]) -> list[dict]:
    """Label each skill: baked (unchanged seed), modified (seed the agent
    edited), learned (not in the seed), deleted (seed the agent removed)."""
    out = []
    for name in sorted(set(stored) | set(seed)):
        if name not in stored:
            origin = "deleted"
        elif name not in seed:
            origin = "learned"
        else:
            origin = "baked" if stored[name] == seed[name] else "modified"
        out.append({"name": name, "origin": origin})
    return out


def classify_tools(baked: list[dict], sandbox_files: list[str]) -> dict:
    """Baked tools are the harness + Python tools; developed tools are runnable
    scripts in the sandbox, grouped by skill folder (or `tools`)."""
    groups: dict[str, list[str]] = {}
    for path in sandbox_files:
        if not path.endswith(SCRIPT_EXTS):
            continue
        parts = path.removeprefix("/workspace/").split("/")
        if parts[0] == "skills" and len(parts) > 2:
            name, rel = parts[1], "/".join(parts[2:])
        else:
            name, rel = parts[0], "/".join(parts[1:])
        groups.setdefault(name, []).append(rel)
    developed = [{"name": n, "files": sorted(f)} for n, f in sorted(groups.items())]
    return {"baked": baked, "developed": developed}


def _seed_skills() -> dict[str, str]:
    if not SEED_SKILLS.is_dir():
        return {}
    return {p.name: p.read_text() for p in sorted(SEED_SKILLS.glob("*")) if p.is_file()}


def list_tools_command() -> str:
    roots = " ".join(shlex.quote(r) for r in TOOL_ROOTS)
    return f"find {roots} -type f 2>/dev/null || true"


async def _user_skills(store, user_id: str) -> dict[str, str]:
    items = await store.asearch(("skills", user_id), limit=1000)
    return {i.key: (i.value or {}).get("content", "") for i in items}


async def snapshot(store, user_ids, baked_tools: list[dict], run_cmd) -> dict:
    seed = _seed_skills()
    skills = {uid: classify_skills(await _user_skills(store, uid), seed) for uid in user_ids}
    res = await run_cmd(list_tools_command())
    files = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
    return {"tools": classify_tools(baked_tools, files), "skills": skills}


async def ensure_table(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute(DDL, prepare=False)


async def catalog_sampler(pool, store, user_ids, baked_tools, run_cmd, interval: float) -> None:
    """Background loop: refresh the single-row catalog snapshot."""
    while True:
        try:
            data = await snapshot(store, user_ids, baked_tools, run_cmd)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO agent_catalog (id, data) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, ts = now()",
                    (json.dumps(data),),
                )
        except Exception:  # noqa: BLE001 - never let the sampler kill the app
            log.exception("catalog sampler error")
        await asyncio.sleep(interval)
