"""Schema setup must survive the pool's prepare_threshold=0 (stdlib-only).

Run without pytest:   python3 tests/test_ddl.py   (from agent/)
Run with pytest:      pytest tests/test_ddl.py
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import scheduler, threads  # noqa: E402


class _PreparingConn:
    """Mimics psycopg with prepare_threshold=0: every execute() is prepared
    (one command only) unless the caller passes prepare=False."""

    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=None, *, prepare=None):
        if prepare is not False and ";" in sql.strip().rstrip(";"):
            raise RuntimeError("cannot insert multiple commands into a prepared statement")
        self.executed.append(sql)


class _FakePool:
    def __init__(self):
        self.conn = _PreparingConn()

    @contextlib.asynccontextmanager
    async def connection(self):
        yield self.conn


def _assert_runs_full_ddl(ensure, ddl):
    pool = _FakePool()
    asyncio.run(ensure(pool))
    assert pool.conn.executed == [ddl]


def test_scheduler_ddl_is_not_prepared():
    _assert_runs_full_ddl(scheduler.ensure_table, scheduler.DDL)


def test_threads_ddl_is_not_prepared():
    _assert_runs_full_ddl(threads.ensure_table, threads.DDL)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
