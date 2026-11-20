"""Practice mode: the agent drills its own skills while you're away.

Toggle per user from chat with `/practice on|off|now|status` (Discord:
`!practice ...`). "on" schedules a recurring job (daily at PRACTICE_AT local
time by default) through the normal scheduler. Each run uses its own throwaway
thread, invents a realistic task for one skill, does it in the sandbox under
/workspace/practice/, and rewrites the skill if it turned out wrong or vague.
A short report is sent to the chat.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo

from app import scheduler
from app.config import settings

SENTINEL = "[[practice]]"


def next_at(now: datetime, tz: tzinfo, hhmm: str) -> datetime:
    """Next UTC instant when local wall-clock time is `hhmm`."""
    h, m = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(tz)
    when = datetime.combine(local.date(), time(h, m), tzinfo=tz)
    if when <= local:
        when = datetime.combine(local.date() + timedelta(days=1), time(h, m), tzinfo=tz)
    return when.astimezone(timezone.utc)


def practice_turn() -> str:
    return (
        "[Practice run, triggered by practice mode, not typed by the user.]\n"
        "Goal: get better at one of your skills by actually doing it.\n"
        "1. List /skills/ and read /workspace/practice/log.md (create it if missing). "
        "Pick the skill practiced least recently (never practiced first).\n"
        "2. Read that skill and invent ONE realistic task a user would plausibly ask "
        "for it. Keep it small enough to finish in a few minutes.\n"
        "3. Do the task by following the skill exactly. Work only in "
        "/workspace/practice/<YYYY-MM-DD>-<skill>/. Don't message anyone, schedule "
        "tasks, install external skills, or touch files outside that folder.\n"
        "4. Grade the result honestly: did following the skill work? Note anything "
        "wrong, missing, outdated, or vague in the steps.\n"
        "5. If the skill needs it, edit /skills/<skill>.md with the smallest fix that "
        "would have helped. If it worked, leave it alone.\n"
        "6. Append one line to /workspace/practice/log.md: "
        "`<date> <skill> <pass|fixed|fail> <one-line note>`.\n"
        "Reply with at most 5 lines: skill, task, result, and what you changed."
    )


async def command(pool, platform: str, user_id: str, chat_id: str, arg: str,
                  *, jobs=None, now: datetime | None = None) -> str:
    """Handle `/practice <arg>`. `jobs` is injectable for tests."""
    add = jobs.add if jobs else scheduler.add_job
    lst = jobs.list if jobs else scheduler.list_jobs
    cancel = jobs.cancel if jobs else scheduler.cancel_job
    now = now or datetime.now(timezone.utc)
    tz = settings.scheduler_tz()
    arg = (arg or "status").strip().lower()

    recurring = [r for r in await lst(pool, platform, user_id) if r[3] == SENTINEL and r[2]]
    if arg == "on":
        for r in recurring:
            await cancel(pool, platform, user_id, r[0])
        when = next_at(now, tz, settings.practice_at)
        await add(pool, platform, user_id, chat_id, SENTINEL, when, settings.practice_every_minutes)
        return (f"Practice is on. Next run {when.astimezone(tz):%a %H:%M} ({tz.key}), then every "
                f"{settings.practice_every_minutes} min. /practice off to stop, /practice now to run one now.")
    if arg == "off":
        for r in recurring:
            await cancel(pool, platform, user_id, r[0])
        return "Practice is off."
    if arg == "now":
        await add(pool, platform, user_id, chat_id, SENTINEL, now + timedelta(minutes=1), None)
        return "Practice run queued; it starts within a minute and I'll report back here."
    if recurring:
        nxt = recurring[0][1].astimezone(tz)
        return f"Practice is on. Next run {nxt:%a %H:%M} ({tz.key}). /practice off to stop."
    return "Practice is off. /practice on to start nightly skill drills, /practice now to run one now."
