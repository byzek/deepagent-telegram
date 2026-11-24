"""Reflection mode: the agent learns from your feedback and its own recent turns.

Feedback comes in three ways:
- 👍 / 👎 reactions on a bot reply (Telegram),
- `/feedback <text>` (Discord: `!feedback`): attached to the replied-to bot
  message, or the latest reply in the chat. Start with `+`/`-` (or 👍/👎) to rate.
- every normal turn is logged (prompt + reply excerpt) as context.

`/reflect on|off|now|status|log|undo [id]` toggles a recurring job (daily at
REFLECT_AT) through the normal scheduler. A run gathers unconsumed feedback +
recent turns, asks the model for concrete edits to `/memories/instructions.md`
and `/skills/*.md`, then gates each edit:
1. path must be one of those targets, content non-empty and under a size cap;
2. an edit that shrinks a file by more than half is rejected (anti-amnesia);
3. a separate judge call compares OLD vs NEW against the cited feedback and the
   edit is applied only if the judge prefers NEW.
Every applied edit is logged with the old content so `/reflect undo` reverts it.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app import practice, scheduler
from app.config import settings

log = logging.getLogger(__name__)

SENTINEL = "[[reflect]]"
POSITIVE = {"👍", "❤", "❤️", "🔥", "👏", "🎉", "💯", "👌"}
NEGATIVE = {"👎", "💩", "🤮", "😢", "🤡", "😡"}
MAX_TURNS_KEPT = 300
_EXCERPT = 1500

DDL = """
CREATE TABLE IF NOT EXISTS turn_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    message_ids BIGINT[] NOT NULL DEFAULT '{}',
    prompt      TEXT NOT NULL,
    reply       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turn_log_user_idx ON turn_log (user_id, id DESC);
CREATE INDEX IF NOT EXISTS turn_log_msg_idx ON turn_log USING GIN (message_ids);
CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    turn_id     BIGINT REFERENCES turn_log(id) ON DELETE SET NULL,
    rating      SMALLINT NOT NULL DEFAULT 0,
    comment     TEXT NOT NULL DEFAULT '',
    consumed    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_user_idx ON feedback (user_id, consumed);
CREATE TABLE IF NOT EXISTS reflection_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    target      TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    reverted    BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_table(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute(DDL, prepare=False)


# --- recording ----------------------------------------------------------------

async def record_turn(pool, user_id: str, chat_id, prompt: str, reply: str,
                      message_ids=()) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO turn_log (user_id, chat_id, message_ids, prompt, reply)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, str(chat_id), [int(m) for m in message_ids],
             prompt[:_EXCERPT], reply[:_EXCERPT]),
        )
        tid = (await cur.fetchone())[0]
        await conn.execute(
            "DELETE FROM turn_log WHERE user_id=%s AND id <= %s", (user_id, tid - MAX_TURNS_KEPT)
        )
        return tid


async def find_turn(pool, user_id: str, chat_id, message_id=None) -> int | None:
    """The turn that produced `message_id`, else the latest turn in the chat."""
    async with pool.connection() as conn:
        if message_id is not None:
            cur = await conn.execute(
                "SELECT id FROM turn_log WHERE user_id=%s AND chat_id=%s AND %s = ANY(message_ids)"
                " ORDER BY id DESC LIMIT 1", (user_id, str(chat_id), int(message_id)))
            row = await cur.fetchone()
            if row:
                return row[0]
        cur = await conn.execute(
            "SELECT id FROM turn_log WHERE user_id=%s AND chat_id=%s ORDER BY id DESC LIMIT 1",
            (user_id, str(chat_id)))
        row = await cur.fetchone()
        return row[0] if row else None


async def add_feedback(pool, user_id: str, turn_id, rating: int, comment: str = "") -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO feedback (user_id, turn_id, rating, comment) VALUES (%s, %s, %s, %s)"
            " RETURNING id", (user_id, turn_id, rating, comment[:2000]))
        return (await cur.fetchone())[0]


def reaction_rating(emojis) -> int:
    """+1 / -1 / 0 for a set of reaction emoji (negative wins ties)."""
    emojis = set(emojis)
    if emojis & NEGATIVE:
        return -1
    return 1 if emojis & POSITIVE else 0


def parse_feedback(text: str) -> tuple[int, str]:
    """`/feedback` body -> (rating, comment). Leading +/-/👍/👎 sets the rating."""
    text = (text or "").strip()
    for marks, rating in ((("+", "👍"), 1), (("-", "👎"), -1)):
        for m in marks:
            if text.startswith(m):
                return rating, text[len(m):].strip()
    return 0, text


async def feedback_command(pool, user_id: str, chat_id, text: str, reply_to_message_id=None) -> str:
    rating, comment = parse_feedback(text)
    if not rating and not comment:
        return ("Usage: /feedback [+|-] <what was good or wrong>. Reply to one of my "
                "messages to attach it there; otherwise it goes on my last reply. "
                "You can also react 👍 / 👎 to any reply.")
    turn = await find_turn(pool, user_id, chat_id, reply_to_message_id)
    await add_feedback(pool, user_id, turn, rating, comment)
    return "Thanks, noted. I'll learn from it on the next /reflect run (or /reflect now)."


# --- reflection -----------------------------------------------------------------

async def _gather(pool, user_id: str) -> tuple[list[dict], list[dict]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT f.id, f.rating, f.comment, t.prompt, t.reply FROM feedback f"
            " LEFT JOIN turn_log t ON t.id = f.turn_id"
            " WHERE f.user_id=%s AND NOT f.consumed ORDER BY f.id LIMIT 50", (user_id,))
        fb = [dict(zip(("id", "rating", "comment", "prompt", "reply"), r))
              for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT prompt, reply FROM turn_log WHERE user_id=%s ORDER BY id DESC LIMIT %s",
            (user_id, settings.reflect_recent_turns))
        turns = [{"prompt": p, "reply": r} for p, r in reversed(await cur.fetchall())]
    return fb, turns


def _content(value) -> str:
    c = (value or {}).get("content", "")
    return "\n".join(c) if isinstance(c, list) else (c or "")


def _split_target(target: str) -> tuple[str, str] | None:
    """`/memories/instructions.md` -> ("memories", "instructions.md"); None if not allowed."""
    if target == "/memories/instructions.md":
        return "memories", "instructions.md"
    m = re.fullmatch(r"/skills/([a-z0-9][a-z0-9._-]{0,80}\.md)", target or "")
    return ("skills", m.group(1)) if m else None


def reflect_prompt(feedback: list[dict], turns: list[dict], instructions: str,
                   skills: dict[str, str]) -> str:
    def fb_line(f):
        tag = {1: "👍", -1: "👎"}.get(f["rating"], "note")
        ctx = f"\n  user asked: {f['prompt']!r}\n  you replied: {f['reply']!r}" if f.get("prompt") else ""
        return f"- [#{f['id']} {tag}] {f['comment'] or '(no comment)'}{ctx}"

    fb = "\n".join(fb_line(f) for f in feedback) or "(none)"
    tl = "\n".join(f"- USER: {t['prompt'][:400]!r}\n  YOU: {t['reply'][:400]!r}" for t in turns) or "(none)"
    sk = "\n\n".join(f"### /skills/{k}\n{v}" for k, v in skills.items()) or "(none)"
    return (
        "You are reviewing your own recent work as a personal assistant to improve "
        "future behaviour. The user's explicit feedback matters most; recent turns are "
        "context for spotting repeated mistakes.\n\n"
        f"## User feedback\n{fb}\n\n## Recent turns\n{tl}\n\n"
        f"## /memories/instructions.md (loaded every turn)\n{instructions or '(empty)'}\n\n"
        f"## Skills\n{sk}\n\n"
        "Propose at most 3 edits that would have prevented the 👎 / corrections or "
        "reinforce what earned 👍. Allowed targets: /memories/instructions.md and "
        "/skills/<name>.md (lowercase, new or existing). Give the COMPLETE new file "
        "content, keep existing useful content, keep files concise. Only propose an "
        "edit when the evidence supports it; zero edits is fine.\n"
        'Reply with ONLY JSON: {"summary": "<one line>", "changes": [{"target": "...", '
        '"content": "...", "rationale": "...", "feedback_ids": [1]}]}'
    )


def judge_prompt(target: str, old: str, new: str, evidence: str) -> str:
    return (
        f"An assistant wants to change its file {target}.\n\n## Evidence\n{evidence}\n\n"
        f"## OLD\n{old or '(file does not exist)'}\n\n## NEW\n{new}\n\n"
        "Judge strictly: is NEW clearly better given the evidence — does it address the "
        "feedback without dropping useful content, adding fluff, or overfitting to one "
        "conversation? Reply with ONLY JSON: "
        '{"winner": "old" | "new", "reason": "<one sentence>"}'
    )


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def static_gate(target: str, old: str, new: str) -> str | None:
    """Reason to reject a change before judging, or None if it passes."""
    if _split_target(target) is None:
        return "target not allowed"
    if not new.strip():
        return "empty content"
    if len(new) > settings.reflect_max_chars:
        return f"over {settings.reflect_max_chars} chars"
    if old and len(new) < len(old) / 2:
        return "drops more than half of the file"
    if new.strip() == old.strip():
        return "no change"
    return None


async def _default_llm(prompt: str) -> str:
    from app.agent import _build_model

    msg = await _build_model().ainvoke([{"role": "user", "content": prompt}])
    c = msg.content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c) \
        if isinstance(c, list) else c


async def run(pool, store, user_id: str, llm=None) -> str:
    """One reflection pass. Returns a short report for the chat."""
    llm = llm or _default_llm
    feedback, turns = await _gather(pool, user_id)
    if not feedback and not turns:
        return "Reflection: nothing to learn from yet."
    item = await store.aget(("memories", user_id), "instructions.md")
    instructions = _content(item.value if item else None)
    skills = {it.key: _content(it.value)
              for it in await store.asearch(("skills", user_id), limit=100)}

    plan = parse_json(await llm(reflect_prompt(feedback, turns, instructions, skills)))
    fb_by_id = {f["id"]: f for f in feedback}
    lines, applied = [], 0
    for ch in (plan.get("changes") or [])[:3]:
        target, new = str(ch.get("target", "")), str(ch.get("content", ""))
        split = _split_target(target)
        old = (instructions if split and split[0] == "memories"
               else skills.get(split[1], "") if split else "")
        why = static_gate(target, old, new)
        if why is None:
            cited = [fb_by_id[i] for i in ch.get("feedback_ids") or [] if i in fb_by_id]
            evidence = "\n".join(
                f"- {'👍' if f['rating'] > 0 else '👎' if f['rating'] < 0 else 'note'} "
                f"{f['comment']} (asked {f.get('prompt')!r})" for f in cited
            ) or f"Rationale: {ch.get('rationale', '')}"
            verdict = parse_json(await llm(judge_prompt(target, old, new, evidence)))
            if str(verdict.get("winner", "")).lower() != "new":
                why = f"judge kept old ({verdict.get('reason', 'no reason')})"
        if why:
            lines.append(f"✗ {target}: {why}")
            continue
        ns, key = split
        prev = await store.aget((ns, user_id), key)
        value = dict(prev.value) if prev else {"encoding": "utf-8"}
        value["content"] = new
        await store.aput((ns, user_id), key, value)
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO reflection_log (user_id, target, old_content, new_content, rationale)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (user_id, target, old if prev else None, new, str(ch.get("rationale", ""))[:500]))
            lid = (await cur.fetchone())[0]
        applied += 1
        lines.append(f"✓ #{lid} {target}: {ch.get('rationale', '')}"[:300])

    if feedback:
        async with pool.connection() as conn:
            await conn.execute("UPDATE feedback SET consumed=true WHERE id = ANY(%s)",
                               ([f["id"] for f in feedback],))
    head = (f"Reflection: {len(feedback)} feedback item(s), {len(turns)} recent turn(s), "
            f"{applied} change(s) applied. {plan.get('summary', '')}").strip()
    tail = "\n/reflect undo <id> reverts a change." if applied else ""
    return "\n".join([head, *lines]) + tail


async def undo(pool, store, user_id: str, log_id: int | None = None) -> str:
    async with pool.connection() as conn:
        q = ("SELECT id, target, old_content FROM reflection_log WHERE user_id=%s AND NOT reverted"
             + (" AND id=%s" if log_id else "") + " ORDER BY id DESC LIMIT 1")
        cur = await conn.execute(q, (user_id, log_id) if log_id else (user_id,))
        row = await cur.fetchone()
        if not row:
            return "Nothing to undo."
        lid, target, old = row
        ns, key = _split_target(target)
        if old is None:
            await store.adelete((ns, user_id), key)
        else:
            prev = await store.aget((ns, user_id), key)
            value = dict(prev.value) if prev else {"encoding": "utf-8"}
            value["content"] = old
            await store.aput((ns, user_id), key, value)
        await conn.execute("UPDATE reflection_log SET reverted=true WHERE id=%s", (lid,))
    return f"Reverted #{lid} ({target})."


async def history(pool, user_id: str, n: int = 10) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, target, rationale, reverted, created_at FROM reflection_log"
            " WHERE user_id=%s ORDER BY id DESC LIMIT %s", (user_id, n))
        rows = await cur.fetchall()
    if not rows:
        return "No reflection changes yet."
    return "\n".join(f"#{i} {t:%m-%d} {tg}{' (reverted)' if rv else ''}: {r[:120]}"
                     for i, tg, r, rv, t in rows)


async def command(pool, store, platform: str, user_id: str, chat_id: str, arg: str,
                  *, jobs=None, now: datetime | None = None) -> str:
    """Handle `/reflect <arg>`. `user_id` is the raw platform id; `jobs` is injectable."""
    add = jobs.add if jobs else scheduler.add_job
    lst = jobs.list if jobs else scheduler.list_jobs
    cancel = jobs.cancel if jobs else scheduler.cancel_job
    now = now or datetime.now(timezone.utc)
    tz = settings.scheduler_tz()
    uid = f"{platform}:{user_id}"
    verb, _, rest = (arg or "status").strip().lower().partition(" ")

    if verb == "undo":
        return await undo(pool, store, uid, int(rest) if rest.strip().isdigit() else None)
    if verb == "log":
        return await history(pool, uid)
    recurring = [r for r in await lst(pool, platform, user_id) if r[3] == SENTINEL and r[2]]
    if verb == "on":
        for r in recurring:
            await cancel(pool, platform, user_id, r[0])
        when = practice.next_at(now, tz, settings.reflect_at)
        await add(pool, platform, user_id, chat_id, SENTINEL, when, settings.reflect_every_minutes)
        return (f"Reflection is on. Next run {when.astimezone(tz):%a %H:%M} ({tz.key}), then every "
                f"{settings.reflect_every_minutes} min. Give feedback with 👍/👎 or /feedback.")
    if verb == "off":
        for r in recurring:
            await cancel(pool, platform, user_id, r[0])
        return "Reflection is off."
    if verb == "now":
        await add(pool, platform, user_id, chat_id, SENTINEL, now + timedelta(minutes=1), None)
        return "Reflection queued; it starts within a minute and I'll report back here."
    state = (f"on, next run {recurring[0][1].astimezone(tz):%a %H:%M} ({tz.key})"
             if recurring else "off")
    return (f"Reflection is {state}. /reflect on|off|now|log|undo [id]. "
            "Feedback: react 👍/👎 or /feedback [+|-] <text>.")
