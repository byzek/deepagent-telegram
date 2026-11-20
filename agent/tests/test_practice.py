"""Unit tests for practice mode (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_practice.py   (from agent/)
Run with pytest:      pytest tests/test_practice.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import practice  # noqa: E402

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)  # 07:00 in New York


def test_next_at_today_or_tomorrow():
    assert practice.next_at(NOW, NY, "09:30") == datetime(2026, 3, 7, 14, 30, tzinfo=timezone.utc)
    assert practice.next_at(NOW, NY, "03:00") == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)  # DST starts


class FakeJobs:
    def __init__(self):
        self.jobs = []

    async def add(self, pool, platform, user_id, chat_id, prompt, when, every):
        self.jobs.append((platform, user_id, chat_id, prompt, every))
        return len(self.jobs)

    async def list(self, pool, platform, user_id):
        return [(i + 1, NOW, j[4], j[3]) for i, j in enumerate(self.jobs) if j[1] == user_id]

    async def cancel(self, pool, platform, user_id, job_id):
        self.jobs[job_id - 1] = (None, None, None, None, None)
        return True


def _run(arg, jobs):
    return asyncio.run(practice.command(None, "telegram", "1", "99", arg, jobs=jobs, now=NOW))


def test_on_off_status_cycle():
    jobs = FakeJobs()
    assert "off" in _run("", jobs).lower()
    assert "on" in _run("on", jobs).lower()
    assert [j[3] for j in jobs.jobs] == [practice.SENTINEL]
    _run("on", jobs)  # idempotent: replaces, doesn't duplicate
    assert sum(j[3] == practice.SENTINEL for j in jobs.jobs) == 1
    assert "on" in _run("status", jobs).lower()
    assert "off" in _run("off", jobs).lower()
    assert all(j[3] != practice.SENTINEL for j in jobs.jobs)


def test_now_queues_one_shot():
    jobs = FakeJobs()
    _run("now", jobs)
    assert jobs.jobs == [("telegram", "1", "99", practice.SENTINEL, None)]


def test_practice_turn_mentions_log_and_skills():
    t = practice.practice_turn()
    assert "/workspace/practice/log.md" in t and "/skills/" in t


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
