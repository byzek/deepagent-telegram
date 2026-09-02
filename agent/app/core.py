"""Assembles the whole runtime: persistence, memory store, agent, metrics.

Transport-agnostic — messaging adapters receive an AppContext and drive it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app import threads
from app.agent import build_agent
from app.config import settings
from app.embeddings import build_embeddings
from app.memory import MemoryStore, build_memory_store
from app.metrics import ensure_metrics_tables
from app.persistence import Persistence, build_persistence
from app.seed import seed_users

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    persistence: Persistence
    mem: MemoryStore
    agent: Any
    memory_backend: str = field(default_factory=lambda: settings.memory_backend)

    @property
    def pool(self):
        return self.persistence.pool

    @property
    def store(self):
        return self.mem.store

    async def close(self) -> None:
        if self.mem.aclose is not None:
            try:
                await self.mem.aclose()
            except Exception:  # noqa: BLE001
                log.exception("memory store close failed")
        await self.persistence.close()


def _seed_user_ids() -> list[str]:
    """Platform-qualified user ids for every allowlisted user."""
    ids: list[str] = []
    for platform in settings.enabled_platforms():
        for uid in settings.allowed_user_ids(platform):
            ids.append(f"{platform}:{uid}")
    return ids


async def build_app_context() -> AppContext:
    persistence = await build_persistence(settings.database_url)
    await ensure_metrics_tables(persistence.pool)
    await threads.ensure_table(persistence.pool)

    embeddings = build_embeddings()
    mem = await build_memory_store(persistence.pool, embeddings)

    await seed_users(mem.store, _seed_user_ids())

    agent = build_agent(persistence.checkpointer, mem.store)

    log.info(
        "%s ready. platforms=%s memory_backend=%s",
        settings.agent_name,
        settings.enabled_platforms(),
        settings.memory_backend,
    )
    return AppContext(persistence=persistence, mem=mem, agent=agent)
