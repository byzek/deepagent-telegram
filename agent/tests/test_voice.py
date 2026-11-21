"""Markdown -> speakable text for TTS (needs httpx importable).

Run without pytest:   python3 tests/test_voice.py   (from agent/)
Run with pytest:      pytest tests/test_voice.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.voice import speakable  # noqa: E402


def test_strips_markdown():
    md = "# Итог\n\n**Хорошо!** See [docs](http://x.y) and `code`.\n- один\n- два\n```py\nprint(1)\n```\nhttps://a.b/c"
    assert speakable(md) == "Итог\nХорошо! See docs and code.\nодин\nдва"


def test_plain_text_untouched():
    assert speakable("Привет, как дела?") == "Привет, как дела?"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
