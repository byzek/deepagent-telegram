"""Unit tests for scheduled-task timing (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_scheduler.py   (from agent/)
Run with pytest:      pytest tests/test_scheduler.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scheduler import first_run, next_run  # noqa: E402

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)  # 07:00 in New York


def _raises(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


def test_first_run_in_minutes():
    assert first_run(NOW, NY, in_minutes=90) == NOW + timedelta(minutes=90)


def test_first_run_naive_iso_is_local_time():
    got = first_run(NOW, NY, run_at="2026-03-07T08:30")
    assert got == datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc)


def test_first_run_aware_iso_keeps_offset():
    got = first_run(NOW, NY, run_at="2026-03-07T20:00:00+00:00")
    assert got == datetime(2026, 3, 7, 20, 0, tzinfo=timezone.utc)


def test_first_run_rejects_past_missing_both_and_garbage():
    assert _raises(first_run, NOW, NY, run_at="2026-03-07T06:00")
    assert _raises(first_run, NOW, NY)
    assert _raises(first_run, NOW, NY, run_at="2026-03-08T08:00", in_minutes=5)
    assert _raises(first_run, NOW, NY, run_at="tomorrow-ish")
    assert _raises(first_run, NOW, NY, in_minutes=0)


def test_next_run_one_shot_is_none():
    assert next_run(NOW, None, NOW, NY) is None


def test_next_run_daily_keeps_wall_clock_across_dst():
    # 08:00 EST on Mar 7 -> 08:00 EDT on Mar 8 (only 23 real hours later).
    prev = datetime(2026, 3, 7, 13, 0, tzinfo=timezone.utc)
    got = next_run(prev, 1440, prev, NY)
    assert got == datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
    assert got.astimezone(NY).hour == 8


def test_next_run_skips_missed_occurrences():
    prev = datetime(2026, 3, 1, 13, 0, tzinfo=timezone.utc)
    now = datetime(2026, 3, 4, 14, 0, tzinfo=timezone.utc)
    got = next_run(prev, 1440, now, NY)
    assert got == datetime(2026, 3, 5, 13, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
