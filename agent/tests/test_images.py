"""Images in and out: Telegram photos reach the model as image blocks, and
images made by `generate_image` ride back with the reply (never the thread).

Run with pytest:      pytest tests/test_images.py   (from agent/)
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.messaging import dispatch  # noqa: E402
from app.messaging.telegram_adapter import pick_photo  # noqa: E402
from app.tools import image  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def test_user_content_text_only_stays_a_string():
    assert dispatch.user_content("hi", []) == "hi"


def test_user_content_with_images_is_blocks():
    blocks = dispatch.user_content("what is this?", [JPEG])
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    url = blocks[1]["image_url"]["url"]
    assert blocks[1]["type"] == "image_url"
    assert url == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_user_content_image_without_caption_has_no_empty_text():
    assert [b["type"] for b in dispatch.user_content("", [JPEG])] == ["image_url"]


def _fake_ctx(on_invoke):
    async def ainvoke(state, config, context):
        on_invoke(state)
        return {"messages": [SimpleNamespace(content="done")]}

    return SimpleNamespace(agent=SimpleNamespace(ainvoke=ainvoke), pool=None,
                           memory_backend="inmemory")


def _patch_io(monkeypatch):
    async def thread_id(pool, platform, chat_id):
        return "t1"

    async def write_turn(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatch.threads, "thread_id", thread_id)
    monkeypatch.setattr(dispatch, "write_turn", write_turn)


def test_handle_turn_sends_images_and_collects_generated(monkeypatch):
    _patch_io(monkeypatch)
    seen = {}

    def on_invoke(state):
        seen["content"] = state["messages"][0]["content"]
        image.outbox.get().append(b"generated")

    reply = asyncio.run(
        dispatch.handle_turn(_fake_ctx(on_invoke), "telegram", 1, 2, "look", images=[JPEG])
    )
    assert [b["type"] for b in seen["content"]] == ["text", "image_url"]
    assert reply.text == "done"
    assert reply.images == [b"generated"]
    assert image.outbox.get() is None


def test_handle_turn_text_only_has_no_images(monkeypatch):
    _patch_io(monkeypatch)
    reply = asyncio.run(dispatch.handle_turn(_fake_ctx(lambda s: None), "telegram", 1, 2, "hi"))
    assert reply == dispatch.Reply("done", [])


def _with_cloudflare(monkeypatch, handler):
    monkeypatch.setattr(image, "settings", dataclasses.replace(
        image.settings, cloudflare_account_id="acct", cloudflare_api_token="tok"))
    real = httpx.AsyncClient
    monkeypatch.setattr(image.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    [tool] = image.build_image_tools()
    return tool


def _run_tool(tool, args):
    async def go():
        box = []
        token = image.outbox.set(box)
        try:
            return await tool.ainvoke(args), box
        finally:
            image.outbox.reset(token)

    return asyncio.run(go())


def test_generate_image_calls_flux_and_fills_outbox(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        body = {"result": {"image": base64.b64encode(JPEG).decode()}, "success": True}
        return httpx.Response(200, json=body)

    tool = _with_cloudflare(monkeypatch, handler)
    out, box = _run_tool(tool, {"prompt": "a red fox", "steps": 50})

    assert box == [JPEG]
    assert "attached" in out
    req = calls[0]
    assert req.url.path.endswith("/accounts/acct/ai/run/@cf/black-forest-labs/flux-1-schnell")
    assert req.headers["authorization"] == "Bearer tok"
    assert json.loads(req.content) == {"prompt": "a red fox", "steps": 8}


def test_generate_image_reports_errors_without_attaching(monkeypatch):
    tool = _with_cloudflare(monkeypatch, lambda r: httpx.Response(429, json={"success": False}))
    out, box = _run_tool(tool, {"prompt": "a red fox"})
    assert out.startswith("IMAGE_ERROR")
    assert box == []


def test_image_tools_disabled_without_credentials(monkeypatch):
    monkeypatch.setattr(image, "settings", dataclasses.replace(
        image.settings, cloudflare_account_id="", cloudflare_api_token=""))
    assert image.build_image_tools() == []


def _size(w, h):
    return SimpleNamespace(width=w, height=h, file_id=f"{w}x{h}")


def test_pick_photo_takes_largest_within_limit():
    sizes = [_size(90, 60), _size(320, 213), _size(800, 533), _size(1280, 853), _size(2560, 1706)]
    assert pick_photo(sizes, 1280).file_id == "1280x853"


def test_pick_photo_falls_back_to_smallest_when_all_too_big():
    assert pick_photo([_size(4000, 3000), _size(2000, 1500)], 1280).file_id == "2000x1500"
