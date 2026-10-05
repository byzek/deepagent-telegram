"""Scheduled tasks: lets the agent wake itself up later.

The agent creates jobs via tools; `scheduler_loop` polls Postgres, runs each due
job as a normal agent turn in the chat that scheduled it, and delivers the reply
through that platform's sender. Recurring jobs advance in the configured local
timezone, so "every day at 08:00" stays at 08:00 across DST changes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo

from app.config import settings

log = logging.getLogger(__name__)

_POLL_SECONDS = 30

DDL = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id            BIGSERIAL PRIMARY KEY,
    platform      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    next_run      TIMESTAMPTZ NOT NULL,
    every_minutes INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scheduled_jobs_next_run ON scheduled_jobs (next_run);
"""


def first_run(now: datetime, tz: tzinfo, run_at: str | None = None,
              in_minutes: int | None = None) -> datetime:
    """Resolve when a new job first fires. Exactly one of `run_at` (ISO 8601;
    naive means local time in `tz`) or `in_minutes` must be given."""
    if (run_at is None) == (in_minutes is None):
        raise ValueError("give exactly one of run_at or in_minutes")
    if in_minutes is not None:
        if in_minutes < 1:
            raise ValueError("in_minutes must be >= 1")
        return now + timedelta(minutes=in_minutes)
    try:
        when = datetime.fromisoformat(run_at)
    except ValueError:
        raise ValueError(f"run_at is not ISO 8601: {run_at!r}") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    when = when.astimezone(timezone.utc)
    if when <= now:
        raise ValueError(f"run_at {run_at!r} is in the past")
    return when


def next_run(prev: datetime, every_minutes: int | None, now: datetime,
             tz: tzinfo) -> datetime | None:
    """Next occurrence after `now` for a recurring job (None for one-shots).
    Steps in local wall-clock time and skips occurrences missed while down."""
    if not every_minutes:
        return None
    step = timedelta(minutes=every_minutes)
    local = prev.astimezone(tz)
    while True:
        local = local + step
        nxt = local.astimezone(timezone.utc)
        if nxt > now:
            return nxt


async def ensure_table(pool) -> None:
    # prepare=False: the pool prepares everything, and prepared statements
    # reject multi-command scripts.
    async with pool.connection() as conn:
        await conn.execute(DDL, prepare=False)


async def add_job(pool, platform: str, user_id: str, chat_id: str, prompt: str,
                  when: datetime, every_minutes: int | None) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO scheduled_jobs"
            " (platform, user_id, chat_id, prompt, next_run, every_minutes)"
            " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (platform, user_id, chat_id, prompt, when, every_minutes),
        )
        return (await cur.fetchone())[0]


async def list_jobs(pool, platform: str, user_id: str) -> list[tuple]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, next_run, every_minutes, prompt FROM scheduled_jobs"
            " WHERE platform=%s AND user_id=%s ORDER BY next_run",
            (platform, user_id),
        )
        return await cur.fetchall()


async def cancel_job(pool, platform: str, user_id: str, job_id: int) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM scheduled_jobs WHERE id=%s AND platform=%s AND user_id=%s",
            (job_id, platform, user_id),
        )
        return cur.rowcount > 0


async def _claim_due(pool, now: datetime, tz: tzinfo) -> list[tuple]:
    """Take every due job and reschedule/delete it BEFORE running, so a crash
    mid-turn never makes a job fire repeatedly."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, platform, user_id, chat_id, prompt, next_run, every_minutes"
                " FROM scheduled_jobs WHERE next_run <= %s FOR UPDATE SKIP LOCKED",
                (now,),
            )
            rows = await cur.fetchall()
            for job_id, *_, prev, every in rows:
                nxt = next_run(prev, every, now, tz)
                if nxt is None:
                    await conn.execute("DELETE FROM scheduled_jobs WHERE id=%s", (job_id,))
                else:
                    await conn.execute(
                        "UPDATE scheduled_jobs SET next_run=%s WHERE id=%s", (nxt, job_id)
                    )
    return rows


async def _fire(ctx, senders: dict, job: tuple) -> None:
    from app.messaging.dispatch import handle_turn

    job_id, platform, user_id, chat_id, prompt, _, every = job
    send = senders.get(platform)
    if send is None or int(user_id) not in settings.allowed_user_ids(platform):
        log.warning("dropping scheduled job #%s: platform/user no longer enabled", job_id)
        return
    kind = f"recurring every {every} min" if every else "one-time"
    text = (
        f"[Scheduled task #{job_id}, {kind}. This was triggered automatically by "
        f"your scheduler, not typed by the user. Carry it out now; your reply is "
        f"sent to the user.]\n{prompt}"
    )
    try:
        reply = await handle_turn(ctx, platform, user_id, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("scheduled job #%s failed", job_id)
        reply = f"Scheduled task #{job_id} failed: {exc}"
    await send(chat_id, reply)


async def scheduler_loop(ctx, senders: dict) -> None:
    """Poll for due jobs forever. `senders` maps platform -> async (chat_id, text)."""
    tz = settings.scheduler_tz()
    running: set[asyncio.Task] = set()
    while True:
        try:
            for job in await _claim_due(ctx.pool, datetime.now(timezone.utc), tz):
                task = asyncio.create_task(_fire(ctx, senders, job))
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception:  # noqa: BLE001 - a DB blip must not kill the scheduler
            log.exception("scheduler poll failed")
        await asyncio.sleep(_POLL_SECONDS)
