"""A text-only model must never be sent image blocks (e.g. from `read_file` on a
.png), or the server 500s and the poisoned thread fails on every later turn.

Run with pytest:      pytest tests/test_model_profile.py   (from agent/)
"""
from __future__ import annotations

import base64
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deepagents.middleware.unsupported_content import (  # noqa: E402
    UnsupportedContentMiddleware,
)
from langchain_core.messages import ToolMessage  # noqa: E402

from app import agent  # noqa: E402


def _image_tool_message() -> ToolMessage:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    return ToolMessage(
        content=[{"type": "image", "base64": png, "mime_type": "image/png"}],
        tool_call_id="call_1",
        additional_kwargs={"read_file_path": "/workspace/shot.png"},
    )


def test_text_only_model_strips_images(monkeypatch):
    monkeypatch.setattr(agent, "settings", dataclasses.replace(agent.settings, llm_vision=False))
    model = agent._build_model()
    out = UnsupportedContentMiddleware()._filter_message(
        _image_tool_message(), model=model
    )
    assert [b["type"] for b in out.content] == ["text"]
    assert "/workspace/shot.png" in out.content[0]["text"]


def test_vision_model_keeps_images(monkeypatch):
    monkeypatch.setattr(agent, "settings", dataclasses.replace(agent.settings, llm_vision=True))
    model = agent._build_model()
    msg = _image_tool_message()
    assert UnsupportedContentMiddleware()._filter_message(msg, model=model) is msg
