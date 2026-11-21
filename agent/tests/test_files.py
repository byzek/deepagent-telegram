"""Attachments: send_file pulls raw bytes from the sandbox's /read endpoint,
send_voice_message queues TTS audio, and both ride back on the Reply."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.messaging import dispatch, telegram_adapter  # noqa: E402
from app.tools import files  # noqa: E402


def _run(tool, args):
    async def go():
        box = []
        token = files.outbox.set(box)
        try:
            return await tool.ainvoke(args), box
        finally:
            files.outbox.reset(token)

    return asyncio.run(go())


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(files.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def _tools(monkeypatch, voice_url=""):
    monkeypatch.setattr(files.voice, "settings",
                        dataclasses.replace(files.voice.settings, voice_url=voice_url))
    return {t.name: t for t in files.build_file_tools()}


def test_guess_kind():
    assert files.guess_kind("a.ogg", 10) == "voice"
    assert files.guess_kind("a.MP3", 10) == "audio"
    assert files.guess_kind("a.png", 10) == "photo"
    assert files.guess_kind("a.png", 11 * 1024 * 1024) == "document"
    assert files.guess_kind("report.pdf", 10) == "document"


def test_send_file_reads_sandbox_and_queues(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"\x00pdf" * 50_000)

    _mock_http(monkeypatch, handler)
    out, box = _run(_tools(monkeypatch)["send_file"],
                    {"path": "/workspace/out/report.pdf", "caption": "here"})
    assert "attached" in out
    [att] = box
    assert (att.kind, att.filename, att.caption) == ("document", "report.pdf", "here")
    assert len(att.data) == 200_000
    assert seen[0].url.path == "/read"
    assert json.loads(seen[0].content) == {"path": "/workspace/out/report.pdf"}


def test_send_file_reports_errors(monkeypatch):
    _mock_http(monkeypatch, lambda r: httpx.Response(404, json={"error": "file_not_found"}))
    out, box = _run(_tools(monkeypatch)["send_file"], {"path": "nope.txt"})
    assert out == "FILE_ERROR: file_not_found" and box == []


def test_voice_tool_only_with_voice_service(monkeypatch):
    assert "send_voice_message" not in _tools(monkeypatch)
    tools = _tools(monkeypatch, "http://voice:8000")

    async def synth(text, speed=1.0):
        return b"OggS" + text.encode()

    monkeypatch.setattr(files.voice, "synthesize", synth)
    out, box = _run(tools["send_voice_message"], {"text": "**Hello** there"})
    assert "attached" in out
    assert box[0].kind == "voice" and box[0].data == b"OggSHello there"


def test_handle_turn_collects_files(monkeypatch):
    async def thread_id(*a):
        return "t1"

    async def write_turn(*a, **k):
        return None

    monkeypatch.setattr(dispatch.threads, "thread_id", thread_id)
    monkeypatch.setattr(dispatch, "write_turn", write_turn)

    async def ainvoke(state, config, context):
        files.outbox.get().append(files.Attachment("document", b"x", "a.txt"))
        return {"messages": [SimpleNamespace(content="ok")]}

    ctx = SimpleNamespace(agent=SimpleNamespace(ainvoke=ainvoke), pool=None, memory_backend="m")
    reply = asyncio.run(dispatch.handle_turn(ctx, "telegram", 1, 2, "hi"))
    assert [a.filename for a in reply.files] == ["a.txt"]
    assert files.outbox.get() is None


def test_telegram_send_attachment_picks_method():
    calls = []

    class Bot:
        def __getattr__(self, name):
            async def f(chat_id, **kw):
                calls.append((name, chat_id, kw.get("filename")))
            return f

    for kind in ("voice", "audio", "photo", "video", "document"):
        asyncio.run(telegram_adapter.send_attachment(Bot(), 5, files.Attachment(kind, b"x", "f")))
    assert [c[0] for c in calls] == ["send_voice", "send_audio", "send_photo",
                                     "send_video", "send_document"]
