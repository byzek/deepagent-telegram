"""Unit tests for reflection mode (stdlib-only, fake pool/store/LLM).

Run without pytest:   python3 tests/test_reflect.py   (from agent/)
Run with pytest:      pytest tests/test_reflect.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import reflect  # noqa: E402

NOW = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
UID = "telegram:1"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class FakeConn:
    """Tiny in-memory stand-in for the three reflect tables."""

    def __init__(self):
        self.turns, self.feedback, self.log = [], [], []

    async def execute(self, sql, params=(), prepare=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO turn_log"):
            self.turns.append(dict(id=len(self.turns) + 1, user_id=params[0], chat_id=params[1],
                                   message_ids=params[2], prompt=params[3], reply=params[4]))
            return FakeCursor([(len(self.turns),)])
        if s.startswith("DELETE FROM turn_log"):
            self.turns = [t for t in self.turns if t["id"] > params[1]]
            return FakeCursor([])
        if s.startswith("SELECT id FROM turn_log"):
            ts = [t for t in self.turns if t["chat_id"] == params[1]]
            if "ANY" in s:
                ts = [t for t in ts if params[2] in t["message_ids"]]
            return FakeCursor([(ts[-1]["id"],)] if ts else [])
        if s.startswith("INSERT INTO feedback"):
            self.feedback.append(dict(id=len(self.feedback) + 1, turn_id=params[1], rating=params[2],
                                      comment=params[3], consumed=False))
            return FakeCursor([(len(self.feedback),)])
        if s.startswith("SELECT f.id"):
            by = {t["id"]: t for t in self.turns}
            return FakeCursor([(f["id"], f["rating"], f["comment"],
                                by.get(f["turn_id"], {}).get("prompt"), by.get(f["turn_id"], {}).get("reply"))
                               for f in self.feedback if not f["consumed"]])
        if s.startswith("SELECT prompt, reply"):
            return FakeCursor([(t["prompt"], t["reply"]) for t in reversed(self.turns)][: params[1]])
        if s.startswith("UPDATE feedback"):
            for f in self.feedback:
                f["consumed"] = f["consumed"] or f["id"] in params[0]
            return FakeCursor([])
        if s.startswith("INSERT INTO reflection_log"):
            self.log.append(dict(id=len(self.log) + 1, target=params[1], old=params[2], reverted=False))
            return FakeCursor([(len(self.log),)])
        if s.startswith("SELECT id, target, old_content"):
            rows = [r for r in self.log if not r["reverted"] and (len(params) == 1 or r["id"] == params[1])]
            return FakeCursor([(rows[-1]["id"], rows[-1]["target"], rows[-1]["old"])] if rows else [])
        if s.startswith("UPDATE reflection_log"):
            self.log[params[0] - 1]["reverted"] = True
            return FakeCursor([])
        raise AssertionError(s)


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    @contextlib.asynccontextmanager
    async def connection(self):
        yield self.conn


class FakeStore:
    def __init__(self):
        self.data = {}

    async def aget(self, ns, key):
        v = self.data.get((ns, key))
        return SimpleNamespace(key=key, value=v) if v is not None else None

    async def aput(self, ns, key, value):
        self.data[(ns, key)] = value

    async def adelete(self, ns, key):
        self.data.pop((ns, key), None)

    async def asearch(self, ns, limit=10):
        return [SimpleNamespace(key=k, value=v) for (n, k), v in self.data.items() if n == ns]


def scripted(*outputs):
    it = iter(outputs)

    async def llm(prompt):
        return next(it)
    return llm


def test_parse_feedback_and_reactions():
    assert reflect.parse_feedback("+ great") == (1, "great")
    assert reflect.parse_feedback("👎 too long") == (-1, "too long")
    assert reflect.parse_feedback("use metric") == (0, "use metric")
    assert reflect.reaction_rating(["👍"]) == 1
    assert reflect.reaction_rating(["👍", "👎"]) == -1
    assert reflect.reaction_rating(["🤔"]) == 0


def test_static_gate():
    assert reflect.static_gate("/etc/passwd", "", "x") == "target not allowed"
    assert reflect.static_gate("/skills/../x.md", "", "x") == "target not allowed"
    assert reflect.static_gate("/skills/a.md", "a" * 100, "short") == "drops more than half of the file"
    assert reflect.static_gate("/memories/instructions.md", "", "be brief") is None


def test_feedback_attaches_to_replied_message():
    pool = FakePool()

    async def go():
        await reflect.record_turn(pool, UID, 9, "q1", "a1", [10])
        await reflect.record_turn(pool, UID, 9, "q2", "a2", [20, 21])
        await reflect.feedback_command(pool, UID, 9, "- wrong", reply_to_message_id=10)
        await reflect.feedback_command(pool, UID, 9, "nice")
    asyncio.run(go())
    assert [(f["turn_id"], f["rating"]) for f in pool.conn.feedback] == [(1, -1), (2, 0)]


def test_run_applies_judged_change_and_undo_reverts():
    pool, store = FakePool(), FakeStore()
    store.data[(("memories", UID), "instructions.md")] = {"content": "Be helpful.", "encoding": "utf-8"}

    async def go():
        t = await reflect.record_turn(pool, UID, 9, "weather?", "It is 70F", [1])
        await reflect.add_feedback(pool, UID, t, -1, "use Celsius")
        plan = json.dumps({"summary": "units", "changes": [
            {"target": "/memories/instructions.md", "content": "Be helpful.\nUse Celsius.",
             "rationale": "user wants metric", "feedback_ids": [1]},
            {"target": "/skills/x.md", "content": "meh", "rationale": "?"},
        ]})
        out = await reflect.run(pool, store, UID, llm=scripted(
            plan, '{"winner": "new", "reason": "fixes it"}', '{"winner": "old", "reason": "fluff"}'))
        assert "1 change(s) applied" in out and "judge kept old" in out, out
        assert store.data[(("memories", UID), "instructions.md")]["content"].endswith("Use Celsius.")
        assert pool.conn.feedback[0]["consumed"]
        assert "Reverted #1" in await reflect.undo(pool, store, UID)
        assert store.data[(("memories", UID), "instructions.md")]["content"] == "Be helpful."
    asyncio.run(go())


def test_command_toggle():
    from tests.test_practice import FakeJobs  # noqa: PLC0415
    jobs = FakeJobs()

    def run(arg):
        return asyncio.run(reflect.command(None, None, "telegram", "1", "99", arg, jobs=jobs, now=NOW))
    assert "off" in run("").lower()
    run("on")
    run("on")
    assert sum(j[3] == reflect.SENTINEL for j in jobs.jobs) == 1
    assert "off" in run("off").lower()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
