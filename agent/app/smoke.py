"""Readiness self-check — verifies the stack is wired up WITHOUT needing a bot.

Run inside the agent container (see `make smoke`). It exercises the real
dependencies end-to-end:

  * Postgres connect + checkpointer/metrics/threads tables
  * memory store setup (pgvector needs the vector extension)
  * embeddings + vector store round-trip (put a memory, semantically recall it)
  * SearXNG reachability
  * sandbox reachability + token auth
  * LLM endpoint reachability (warning only)

Exits non-zero if any critical check fails, so it's CI/pre-flight friendly.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import settings
from app.embeddings import build_embeddings
from app.memory import build_memory_store
from app.metrics import ensure_metrics_tables
from app.persistence import build_persistence
from app import threads

_GREEN = "\033[32m"
_RED = "\033[31m"
_YEL = "\033[33m"
_RST = "\033[0m"

_NS = ("smoke", "selfcheck")
_KEY = "fact.txt"
_DOC = "The user's favorite color is teal and they live in Denver, Colorado."
_QUERY = "which city does the user live in?"


class Result:
    def __init__(self):
        self.rows: list[tuple[str, bool, bool, str]] = []  # name, ok, critical, detail
        self.failed_critical = False

    def add(self, name: str, ok: bool, critical: bool, detail: str = "") -> None:
        self.rows.append((name, ok, critical, detail))
        if critical and not ok:
            self.failed_critical = True

    def render(self) -> None:
        print("\n== deepagent readiness ==")
        for name, ok, critical, detail in self.rows:
            if ok:
                mark, color = "PASS", _GREEN
            elif critical:
                mark, color = "FAIL", _RED
            else:
                mark, color = "WARN", _YEL
            line = f"  {color}{mark}{_RST}  {name}"
            if detail:
                line += f"  — {detail}"
            print(line)
        print()


async def _check_llm(res: Result) -> None:
    try:
        headers = {}
        if settings.llm_api_key and settings.llm_api_key != "not-needed":
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{settings.llm_base_url}/models", headers=headers)
            r.raise_for_status()
        res.add("llm endpoint (/v1/models)", True, critical=False, detail=settings.llm_base_url)
    except Exception as exc:  # noqa: BLE001
        res.add("llm endpoint (/v1/models)", False, critical=False, detail=str(exc))


async def _check_searxng(res: Result) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{settings.searxng_url}/search",
                params={"q": "ping", "format": "json"},
            )
            r.raise_for_status()
            r.json()
        res.add("searxng search", True, critical=True)
    except Exception as exc:  # noqa: BLE001
        res.add("searxng search", False, critical=True, detail=str(exc))


async def _check_sandbox(res: Result) -> None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{settings.sandbox_url}/execute",
                json={"command": "echo smoke-ok"},
                headers={"X-Sandbox-Token": settings.sandbox_token},
            )
            r.raise_for_status()
            data = r.json()
        ok = "smoke-ok" in (data.get("stdout") or "")
        res.add("sandbox execute", ok, critical=True,
                detail="" if ok else f"unexpected output: {data}")
    except Exception as exc:  # noqa: BLE001
        res.add("sandbox execute", False, critical=True, detail=str(exc))


async def _check_memory(res: Result) -> None:
    """DB + store setup + embeddings + semantic recall round-trip."""
    persistence = None
    mem = None
    try:
        persistence = await build_persistence(settings.database_url)
        await ensure_metrics_tables(persistence.pool)
        await threads.ensure_table(persistence.pool)
        res.add("postgres + checkpointer/tables", True, critical=True)
    except Exception as exc:  # noqa: BLE001
        res.add("postgres + checkpointer/tables", False, critical=True, detail=str(exc))
        return

    try:
        embeddings = build_embeddings()
        mem = await build_memory_store(persistence.pool, embeddings)
        res.add(f"memory store setup ({settings.memory_backend})", True, critical=True)
    except Exception as exc:  # noqa: BLE001
        res.add(f"memory store setup ({settings.memory_backend})", False, critical=True,
                detail=str(exc))
        await persistence.close()
        return

    try:
        store = mem.store
        await store.aput(_NS, _KEY, {"content": _DOC})
        items = await store.asearch(_NS, query=_QUERY, limit=1)
        hit = bool(items) and "denver" in (
            (items[0].value or {}).get("content", "").lower()
            if isinstance(items[0].value, dict) else ""
        )
        res.add("embeddings + semantic recall", hit, critical=True,
                detail="" if hit else "no relevant match returned")
    except Exception as exc:  # noqa: BLE001
        res.add("embeddings + semantic recall", False, critical=True, detail=str(exc))
    finally:
        try:
            await mem.store.adelete(_NS, _KEY)
        except Exception:  # noqa: BLE001
            pass
        if mem.aclose is not None:
            try:
                await mem.aclose()
            except Exception:  # noqa: BLE001
                pass
        await persistence.close()


async def amain() -> int:
    res = Result()
    # Memory chain first (it owns the DB pool lifecycle); then the HTTP checks.
    await _check_memory(res)
    await _check_searxng(res)
    await _check_sandbox(res)
    await _check_llm(res)
    res.render()

    if res.failed_critical:
        print(f"{_RED}Readiness check FAILED.{_RST} Fix the FAIL rows above before connecting a bot.")
        return 1
    print(f"{_GREEN}Readiness check passed.{_RST} You're clear to bring up the agent.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
