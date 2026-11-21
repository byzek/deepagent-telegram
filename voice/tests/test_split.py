"""Script splitting for bilingual TTS (stdlib-only).

Run:  python3 tests/test_split.py   (from voice/)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_server import split_by_script  # noqa: E402


def test_pure_russian():
    assert split_by_script("Привет, как дела?") == [("ru", "Привет, как дела?")]


def test_code_switch():
    assert split_by_script("Скажи «hedging my bets» ещё раз.") == [
        ("ru", "Скажи «"), ("en", "hedging my bets»"), ("ru", "ещё раз."),
    ]


def test_numbers_only_use_default():
    assert split_by_script("42!") == [("ru", "42!")]
    assert split_by_script("   ") == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
