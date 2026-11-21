"""Unit tests for Russian practice mode (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_russian.py   (from agent/)
Run with pytest:      pytest tests/test_russian.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import russian  # noqa: E402

NOW = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)


def test_sm2_progression():
    ease, interval, reps, lapsed = russian.sm2(2.5, 0, 0, 4)
    assert (interval, reps, lapsed) == (1, 1, False)
    ease, interval, reps, _ = russian.sm2(ease, interval, reps, 4)
    assert (interval, reps) == (3, 2)
    ease, interval, reps, _ = russian.sm2(ease, interval, reps, 5)
    assert interval >= 7 and reps == 3


def test_sm2_lapse_resets_and_floors_ease():
    ease, interval, reps, lapsed = russian.sm2(1.3, 40, 6, 0)
    assert (interval, reps, lapsed) == (1, 0, True)
    assert ease == 1.3
    assert russian.sm2(2.5, 0, 0, 99)[0] <= 2.6  # grade clamped to 5


def test_transcript_skips_setup_and_tools():
    msgs = [
        SimpleNamespace(type="human", content="[Russian practice session, started by /russian.] rules"),
        SimpleNamespace(type="ai", content="Привет! Как дела?"),
        SimpleNamespace(type="human", content="[Russian session, voice] Хорошо, спасибо."),
        SimpleNamespace(type="tool", content="Added card #1"),
        SimpleNamespace(type="ai", content=[{"type": "text", "text": "Отлично."}]),
    ]
    assert russian.format_transcript(msgs) == (
        "TUTOR: Привет! Как дела?\nUSER: Хорошо, спасибо.\nTUTOR: Отлично."
    )
    assert russian.format_transcript(msgs, limit=10).startswith("...\n")


def test_prompts_mention_cards_and_topic():
    text = russian.opening_turn([(7, "перестраховаться", "to play it safe", "")], "a lease")
    assert "#7: перестраховаться = to play it safe" in text and "a lease" in text
    assert "straight into the roleplay" in russian.opening_turn([], "x")
    assert russian.user_turn("да", voice=True) == "[Russian session, voice] да"
    assert russian.topic_for(NOW) in russian.TOPICS


class FakeJobs:
    def __init__(self):
        self.jobs = []

    async def add(self, pool, platform, user_id, chat_id, prompt, when, every):
        self.jobs.append((platform, user_id, chat_id, prompt, when, every))
        return len(self.jobs)

    async def list(self, pool, platform, user_id):
        return [(i + 1, j[4], j[5], j[3]) for i, j in enumerate(self.jobs) if j[3]]

    async def cancel(self, pool, platform, user_id, job_id):
        self.jobs[job_id - 1] = (None,) * 6
        return True


class FakeConn:
    """Answers the few queries `command`/`route` make from canned state."""

    def __init__(self, state):
        self.state = state

    async def execute(self, sql, params=None, **_):
        s = self.state
        if sql.startswith("DELETE FROM russian_sessions"):
            row, s["session"] = s["session"], None
            return SimpleNamespace(fetchone=_async(row and row[:4]))
        if sql.startswith("SELECT user_id, thread_id"):
            return SimpleNamespace(fetchone=_async(s["session"]))
        if sql.startswith("SELECT count(*)"):
            return SimpleNamespace(fetchone=_async((2, 5)))
        if sql.startswith("INSERT INTO scheduled_jobs"):
            s["graded"] = params[3]
            return SimpleNamespace(fetchone=_async((1,)))
        if sql.startswith("UPDATE russian_sessions"):
            s["touched"] = True
        return SimpleNamespace(fetchone=_async(None))


def _async(value):
    async def f():
        return value
    return f


class FakePool:
    def __init__(self, session=None):
        self.state = {"session": session}

    @contextlib.asynccontextmanager
    async def connection(self):
        yield FakeConn(self.state)


def _cmd(arg, jobs, pool=None):
    return asyncio.run(russian.command(pool or FakePool(), "telegram", "1", "1", arg, jobs=jobs, now=NOW))


def test_command_on_off_now():
    jobs = FakeJobs()
    assert "is on" in _cmd("on", jobs)
    assert "is on" in _cmd("on", jobs)  # re-enabling replaces, doesn't duplicate
    assert len([j for j in jobs.jobs if j[3] == russian.SENTINEL]) == 1
    assert "next session" in _cmd("status", jobs) and "2 due / 5 total" in _cmd("status", jobs)
    assert "off" in _cmd("off", jobs)
    assert not [j for j in jobs.jobs if j[3]]
    _cmd("now", jobs)
    assert jobs.jobs[-1][3:] == (russian.SENTINEL, NOW, None)
    assert "Usage" in _cmd("bogus", jobs)


def test_stop_queues_grading_only_when_talked():
    jobs = FakeJobs()
    pool = FakePool(("1", "russian:t:1:x", [3, 4], 2, NOW))
    assert "grading" in _cmd("stop", jobs, pool)
    assert jobs.jobs[-1][3] == f"{russian.GRADE} russian:t:1:x 3,4"
    assert "No Russian session" in _cmd("stop", jobs, pool)

    jobs = FakeJobs()
    _cmd("stop", jobs, FakePool(("1", "russian:t:1:y", [], 0, NOW)))
    assert jobs.jobs == []


def test_route_active_vs_expired():
    pool = FakePool(("1", "russian:t:1:x", [], 1, NOW))
    assert asyncio.run(russian.route(pool, "telegram", "1", NOW + timedelta(minutes=5))) == "russian:t:1:x"
    assert pool.state.get("touched")
    late = NOW + timedelta(minutes=russian.settings.russian_idle_minutes + 1)
    assert asyncio.run(russian.route(pool, "telegram", "1", late)) is None
    assert pool.state["session"] is None
    assert pool.state["graded"] == f"{russian.GRADE} russian:t:1:x"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
