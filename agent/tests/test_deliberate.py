"""Unit tests for deliberate mode helpers (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_deliberate.py   (from agent/)
Run with pytest:      pytest tests/test_deliberate.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.deliberate import judge_prompt, pick_index, split_trigger, wrap_turn  # noqa: E402


def test_split_trigger_variants():
    for msg in ("deliberate: why X?", "Deliberate why X?", "/deliberate why X?",
                "!deliberate why X?", "deliberate, why X?", "/deliberate@MyBot why X?"):
        assert split_trigger(msg) == (True, "why X?"), msg


def test_split_trigger_ignores_other_words():
    assert split_trigger("deliberately vague") == (False, "deliberately vague")
    assert split_trigger("please deliberate") == (False, "please deliberate")


def test_pick_index_parses_json_and_falls_back():
    assert pick_index('{"best": 3, "reason": "x"}', 4) == (3, "x")
    assert pick_index('blah ```json\n{"best": 2}\n```', 4) == (2, "")
    assert pick_index("Answer 4 is best", 4) == (4, "Answer 4 is best")
    assert pick_index('{"best": 9}', 4) == (1, "judge gave no valid choice")
    assert pick_index("", 4) == (1, "judge gave no valid choice")


def test_judge_prompt_numbers_candidates():
    p = judge_prompt("Q?", "ctx", ["a", "b"])
    assert "### Answer 1\na" in p and "### Answer 2\nb" in p and "Q?" in p


def test_wrap_turn_tells_agent_to_call_tool():
    w = wrap_turn("Q?")
    assert "deliberate(" in w and w.endswith("Q?")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
