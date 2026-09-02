"""Postgres connection pool + durable checkpointer.

The checkpointer holds conversation state (message history, resumable runs) and
is ALWAYS Postgres. The long-term memory *store* is separate and swappable —
see app/memory. Both the checkpointer and the pgvector store can share this
pool.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


@dataclass
class Persistence:
    pool: AsyncConnectionPool
    checkpointer: AsyncPostgresSaver

    async def close(self) -> None:
        await self.pool.close()


async def build_persistence(database_url: str, *, max_size: int = 10) -> Persistence:
    pool = AsyncConnectionPool(
        conninfo=database_url,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()

    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    log.info("Postgres checkpointer ready.")

    return Persistence(pool=pool, checkpointer=checkpointer)
