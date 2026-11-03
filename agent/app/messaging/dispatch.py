"""Transport-agnostic turn handling shared by all messaging adapters.

An adapter authenticates the sender, then calls `handle_turn(...)`; this module
runs the agent, records metrics, and returns the reply text plus any images the
agent generated. Adapters only deal with platform I/O (and their own
message-length limits via `chunk`).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field

from app import threads
from app.agent import Context
from app.config import settings
from app.metrics import TurnMetrics, write_turn
from app.tools.image import outbox

log = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str
    images: list[bytes] = field(default_factory=list)


def user_content(text: str, images) -> str | list[dict]:
    """The user message content: plain text, or OpenAI-style blocks with each
    JPEG inlined as a data URL (Halogen refuses http(s) image URLs)."""
    if not images:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    for data in images:
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return blocks


def extract_text(result) -> str:
    msg = result["messages"][-1]
    content = getattr(msg, "content", msg)
    if isinstance(content, list):  # provider returned content blocks
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return content or ""


def chunk(text: str, size: int) -> list[str]:
    if not text:
        return ["(empty response)"]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def handle_turn(
    ctx, platform: str, raw_user_id, chat_id, text: str, images=()
) -> Reply:
    """Run one user message (optionally with JPEG images) through the agent and
    return the reply."""
    user_id = f"{platform}:{raw_user_id}"
    tid = await threads.thread_id(ctx.pool, platform, chat_id)

    metrics = TurnMetrics()
    config = {
        "configurable": {"thread_id": tid},
        "recursion_limit": 1000,
        "callbacks": [metrics],
    }
    # The per-token idle timeout (on the model) keeps us waiting while the LLM is
    # actually producing output; this is the absolute safety ceiling for the
    # whole turn regardless of activity. 0 disables it (wait indefinitely).
    generated: list[bytes] = []
    token = outbox.set(generated)
    try:
        result = await asyncio.wait_for(
            ctx.agent.ainvoke(
                {"messages": [{"role": "user", "content": user_content(text, images)}]},
                config=config,
                context=Context(user_id=user_id, chat_id=str(chat_id)),
            ),
            timeout=settings.llm_hard_timeout or None,
        )
    finally:
        outbox.reset(token)
    reply = Reply(extract_text(result), generated)

    try:
        await write_turn(
            ctx.pool,
            user_id=user_id,
            platform=platform,
            memory_backend=ctx.memory_backend,
            model=settings.llm_model,
            m=metrics,
        )
    except Exception:  # noqa: BLE001 - metrics must never break a reply
        log.exception("failed to write turn metrics")

    return reply
