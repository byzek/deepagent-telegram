"""Russian conversation practice over Telegram voice notes.

`/russian on` schedules a daily session (RUSSIAN_AT local time). A session runs
in its own thread so it doesn't clutter the normal chat: the bot opens with a
spoken quiz on due flashcards, then a short roleplay. While a session is
active every reply in that chat is spoken. When you switch to English for
help, the agent explains, drills it, and saves a flashcard.

A session ends on `/russian stop`, when a new one starts, or on the first
message after RUSSIAN_IDLE_MINUTES of silence. Ending queues a background
grading run that reads the transcript, reschedules the reviewed cards, adds
cards for mistakes, and sends a short text summary.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import practice, scheduler
from app.config import settings

SENTINEL = "[[russian]]"
GRADE = "[[russian-grade]]"

DDL = """
CREATE TABLE IF NOT EXISTS russian_sessions (
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    card_ids   BIGINT[] NOT NULL DEFAULT '{}',
    turns      INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, chat_id)
);
CREATE TABLE IF NOT EXISTS russian_cards (
    id            BIGSERIAL PRIMARY KEY,
    owner         TEXT NOT NULL,
    front         TEXT NOT NULL,
    back          TEXT NOT NULL,
    example       TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    ease          REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 0,
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    due           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS russian_cards_owner_front ON russian_cards (owner, lower(front));
CREATE INDEX IF NOT EXISTS russian_cards_due ON russian_cards (owner, due);
"""

TOPICS = [
    "negotiating a flat lease with a landlord who wants a higher deposit",
    "explaining your job to a curious relative who doesn't work in tech",
    "arguing about a book or film you both have opinions on",
    "ordering at a busy café and then complaining that the order is wrong",
    "telling a friend what actually happened at work today",
    "asking a pharmacist for advice about a cold",
    "planning a weekend trip and disagreeing about the budget",
    "a job interview where they ask about your weaknesses",
    "returning a broken item to a shop without a receipt",
    "small talk with a neighbour about the building's renovation",
]


async def ensure_table(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute(DDL, prepare=False)


# --- spaced repetition (SM-2, tuned slightly shorter for speaking practice) ---

def sm2(ease: float, interval: int, reps: int, grade: int) -> tuple[float, int, int, bool]:
    """Return (ease, interval_days, reps, lapsed) after a review graded 0-5
    (0 = no idea, 3 = got it with effort, 5 = instant)."""
    grade = max(0, min(5, grade))
    ease = max(1.3, ease + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    if grade < 3:
        return ease, 1, 0, True
    reps += 1
    interval = 1 if reps == 1 else 3 if reps == 2 else max(interval + 1, round(interval * ease))
    return ease, interval, reps, False


async def due_cards(pool, owner: str, limit: int, now: datetime | None = None) -> list[tuple]:
    """(id, front, back, example) for cards due now, most overdue first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, front, back, example FROM russian_cards"
            " WHERE owner=%s AND due <= %s ORDER BY due LIMIT %s",
            (owner, now or datetime.now(timezone.utc), limit),
        )
        return await cur.fetchall()


async def add_card(pool, owner: str, front: str, back: str, example: str = "", note: str = "") -> tuple[int, bool]:
    """Insert or refresh a card (same front, case-insensitive). Returns (id, created).
    Re-adding an existing card makes it due again."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO russian_cards (owner, front, back, example, note) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (owner, lower(front)) DO UPDATE SET back=EXCLUDED.back,"
            " example=COALESCE(NULLIF(EXCLUDED.example, ''), russian_cards.example),"
            " note=COALESCE(NULLIF(EXCLUDED.note, ''), russian_cards.note), due=now()"
            " RETURNING id, (xmax = 0)",
            (owner, front.strip(), back.strip(), example.strip(), note.strip()),
        )
        row = await cur.fetchone()
        return row[0], row[1]


async def review_card(pool, owner: str, card_id: int, grade: int, now: datetime | None = None) -> datetime | None:
    """Apply a review; returns the new due time (None if no such card)."""
    now = now or datetime.now(timezone.utc)
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT ease, interval_days, reps FROM russian_cards WHERE id=%s AND owner=%s FOR UPDATE",
                (card_id, owner),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            ease, interval, reps, lapsed = sm2(row[0], row[1], row[2], grade)
            due = now + timedelta(days=interval)
            await conn.execute(
                "UPDATE russian_cards SET ease=%s, interval_days=%s, reps=%s,"
                " lapses=lapses + %s, due=%s WHERE id=%s",
                (ease, interval, reps, int(lapsed), due, card_id),
            )
            return due


async def card_counts(pool, owner: str, now: datetime | None = None) -> tuple[int, int]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FILTER (WHERE due <= %s), count(*) FROM russian_cards WHERE owner=%s",
            (now or datetime.now(timezone.utc), owner),
        )
        return tuple(await cur.fetchone())


# --- sessions -----------------------------------------------------------------

async def _get_session(pool, platform: str, chat_id) -> tuple | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, thread_id, card_ids, turns, last_at FROM russian_sessions"
            " WHERE platform=%s AND chat_id=%s",
            (platform, str(chat_id)),
        )
        return await cur.fetchone()


async def end_session(pool, platform: str, chat_id, now: datetime | None = None, *, add_job=None) -> bool:
    """End the chat's session (if any) and queue grading when anything was said.
    Returns whether a session existed."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM russian_sessions WHERE platform=%s AND chat_id=%s"
            " RETURNING user_id, thread_id, card_ids, turns",
            (platform, str(chat_id)),
        )
        row = await cur.fetchone()
    if row is None:
        return False
    user_id, thread, card_ids, turns = row
    if turns > 0:
        prompt = f"{GRADE} {thread} {','.join(map(str, card_ids))}".rstrip()
        await (add_job or scheduler.add_job)(
            pool, platform, user_id, str(chat_id), prompt, now or datetime.now(timezone.utc), None
        )
    return True


async def start_session(pool, platform: str, user_id: str, chat_id, card_ids: list[int],
                        now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    await end_session(pool, platform, chat_id, now)
    thread = f"russian:{platform}:{chat_id}:{now:%Y%m%dT%H%M%S}"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO russian_sessions (platform, chat_id, user_id, thread_id, card_ids, started_at, last_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (platform, str(chat_id), str(user_id), thread, card_ids, now, now),
        )
    return thread


def is_expired(last_at: datetime, now: datetime) -> bool:
    return now - last_at > timedelta(minutes=settings.russian_idle_minutes)


async def route(pool, platform: str, chat_id, now: datetime | None = None) -> str | None:
    """Thread id to use for an incoming message, or None for the normal chat.
    Expired sessions are ended (and graded) here."""
    now = now or datetime.now(timezone.utc)
    row = await _get_session(pool, platform, chat_id)
    if row is None:
        return None
    if is_expired(row[4], now):
        await end_session(pool, platform, chat_id, now)
        return None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE russian_sessions SET turns=turns + 1, last_at=%s WHERE platform=%s AND chat_id=%s",
            (now, platform, str(chat_id)),
        )
    return row[1]


# --- prompts ------------------------------------------------------------------

def topic_for(day: datetime) -> str:
    return TOPICS[day.toordinal() % len(TOPICS)]


def opening_turn(cards: list[tuple], topic: str) -> str:
    if cards:
        deck = "\n".join(f"- #{cid}: {front} = {back}" + (f" (e.g. {ex})" if ex else "")
                         for cid, front, back, ex in cards)
        quiz = (f"Start with a quick quiz on these due flashcards, one at a time: say the English, "
                f"the user answers in Russian.\n{deck}\nAfter the quiz, move into the roleplay.")
    else:
        quiz = "No flashcards are due, so go straight into the roleplay."
    return (
        "[Russian practice session, started by /russian. Not typed by the user.]\n"
        "You are a friendly native Russian conversation partner. The user is learning Russian "
        "and talks to you by Telegram voice notes; your replies are read aloud by TTS.\n"
        "Rules for the whole session:\n"
        "- Speak Russian, at the user's level. Keep each reply to 1-3 short sentences and end "
        "with a question or prompt so the conversation keeps going.\n"
        "- Plain text only: no Markdown, lists, emoji, transliteration, or stress marks.\n"
        "- Don't correct every mistake. Gently recast at most one important error per reply "
        "(say the correct version naturally) and carry on.\n"
        "- If the user switches to English or asks how to say something: answer briefly in "
        "English, give the natural (not textbook) Russian phrasing, ask them to say it back "
        "twice, and call `russian_add_card` to save it. Then return to Russian.\n"
        "- User messages marked 'voice' are Whisper transcripts: judge words and grammar, "
        "not accent, and ignore obvious transcription glitches.\n"
        "- If the user says stop/хватит/пока, say a short goodbye and tell them to send "
        "/russian stop to get their summary.\n"
        f"{quiz}\n"
        f"Roleplay scenario for today: {topic}. Set the scene in one sentence, then begin.\n"
        "Now greet the user in Russian and start."
    )


def user_turn(text: str, *, voice: bool) -> str:
    return f"[Russian session, {'voice' if voice else 'typed'}] {text}"


def grade_turn(transcript: str, cards: list[tuple]) -> str:
    deck = "\n".join(f"- #{cid}: {front} = {back}" for cid, front, back in cards) or "(none)"
    return (
        "[Russian session grading, a background job. Not typed by the user.]\n"
        "Below is the transcript of a Russian practice session (USER lines are Whisper "
        "transcripts of voice notes).\n"
        f"Flashcards that were quizzed:\n{deck}\n\n"
        "Do this:\n"
        "1. For each quizzed card, call `russian_review_card` with a 0-5 grade based on how "
        "the user answered (0 = no idea or skipped, 3 = right after hesitation or a hint, "
        "5 = instant and correct).\n"
        "2. Find the user's 3-6 most useful mistakes or gaps (grammar, word choice, missing "
        "words they reached for in English). For each, call `russian_add_card` with the "
        "correct Russian, the English meaning, and a short example. Skip anything already "
        "saved during the session.\n"
        "3. Save a one-line note to memory about the user's level and recurring weak spots, "
        "if it adds something new.\n"
        "4. Reply with a short summary in English (max 8 lines): what went well, the main "
        "corrections as `wrong -> right`, and how many cards were added or reviewed.\n\n"
        f"TRANSCRIPT\n{transcript}"
    )


def _content_text(content) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def format_transcript(messages, limit: int = 30000) -> str:
    lines = []
    for m in messages:
        kind = getattr(m, "type", "")
        text = _content_text(getattr(m, "content", "")).strip()
        if not text or kind not in ("human", "ai"):
            continue
        if kind == "human":
            if text.startswith("[Russian practice session"):
                continue
            text = text.split("] ", 1)[1] if text.startswith("[Russian session,") else text
        lines.append(f"{'USER' if kind == 'human' else 'TUTOR'}: {text}")
    out = "\n".join(lines)
    return out if len(out) <= limit else "...\n" + out[-limit:]


async def scheduled(ctx, platform: str, user_id: str, chat_id: str, prompt: str):
    """Scheduler hook: (text, thread, voice) for Russian jobs, else None."""
    if prompt == SENTINEL:
        owner = f"{platform}:{user_id}"
        cards = await due_cards(ctx.pool, owner, settings.russian_review_cards)
        thread = await start_session(ctx.pool, platform, user_id, chat_id, [c[0] for c in cards])
        return opening_turn(cards, topic_for(datetime.now(settings.scheduler_tz()))), thread, True
    if prompt.startswith(GRADE):
        parts = prompt.split()
        thread = parts[1]
        ids = [int(x) for x in parts[2].split(",")] if len(parts) > 2 else []
        state = await ctx.agent.aget_state({"configurable": {"thread_id": thread}})
        transcript = format_transcript(state.values.get("messages", []))
        async with ctx.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, front, back FROM russian_cards WHERE id = ANY(%s) AND owner=%s",
                (ids, f"{platform}:{user_id}"),
            )
            cards = await cur.fetchall()
        return grade_turn(transcript, cards), f"{thread}:grade", False
    return None


# --- /russian command -----------------------------------------------------------

async def command(pool, platform: str, user_id: str, chat_id: str, arg: str,
                  *, jobs=None, now: datetime | None = None) -> str:
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
        when = practice.next_at(now, tz, settings.russian_at)
        await add(pool, platform, user_id, chat_id, SENTINEL, when, settings.russian_every_minutes)
        return (f"Russian practice is on. Next session {when.astimezone(tz):%a %H:%M} ({tz.key}). "
                "/russian now to start one right away.")
    if arg == "off":
        for r in recurring:
            await cancel(pool, platform, user_id, r[0])
        return "Russian practice schedule is off. /russian now still starts a one-off session."
    if arg == "now":
        await add(pool, platform, user_id, chat_id, SENTINEL, now, None)
        return "Starting a Russian session, I'll send a voice note in a moment. Reply with voice notes."
    if arg == "stop":
        if await end_session(pool, platform, chat_id, now, add_job=add):
            return "Session ended. I'm grading it now and will send a summary shortly."
        return "No Russian session is active."
    if arg not in ("status", ""):
        return "Usage: /russian on | off | now | stop | status"

    due, total = await card_counts(pool, f"{platform}:{user_id}", now)
    sched = (f"next session {recurring[0][1].astimezone(tz):%a %H:%M} ({tz.key})"
             if recurring else "no daily session scheduled (/russian on)")
    active = await _get_session(pool, platform, chat_id)
    live = "A session is active (/russian stop to end it). " if active and not is_expired(active[4], now) else ""
    return f"{live}Russian: {sched}. Flashcards: {due} due / {total} total."
