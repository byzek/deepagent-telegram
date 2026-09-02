"""Per-chat conversation thread tracking (backend-independent).

Stored in Postgres so it works the same regardless of which memory backend is
selected. A thread id is `<platform>:<chat_id>:<epoch>`; /reset bumps the epoch
to start a fresh conversation while keeping long-term memory intact.
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS chat_threads (
    platform TEXT NOT NULL,
    chat_id  TEXT NOT NULL,
    epoch    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, chat_id)
);
"""


async def ensure_table(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute(DDL)


async def thread_id(pool, platform: str, chat_id) -> str:
    chat = str(chat_id)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO chat_threads (platform, chat_id) VALUES (%s,%s)"
            " ON CONFLICT DO NOTHING",
            (platform, chat),
        )
        cur = await conn.execute(
            "SELECT epoch FROM chat_threads WHERE platform=%s AND chat_id=%s",
            (platform, chat),
        )
        row = await cur.fetchone()
    epoch = row[0] if row else 0
    return f"{platform}:{chat}:{epoch}"


async def reset(pool, platform: str, chat_id) -> None:
    chat = str(chat_id)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO chat_threads (platform, chat_id, epoch) VALUES (%s,%s,1)"
            " ON CONFLICT (platform, chat_id) DO UPDATE SET epoch = chat_threads.epoch + 1",
            (platform, chat),
        )
