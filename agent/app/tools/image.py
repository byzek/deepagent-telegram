"""Image generation via Cloudflare Workers AI (FLUX.1 [schnell]).

Generated images never enter the conversation: `handle_turn` opens an `outbox`
for each turn, `generate_image` appends the bytes to it, and the messaging
adapter sends whatever is in it as photos alongside the reply. Only a short
status line goes back to the model (and into the checkpointed thread).
"""
from __future__ import annotations

import base64
import logging
from contextvars import ContextVar

import httpx
from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger(__name__)

_MODEL = "@cf/black-forest-labs/flux-1-schnell"
_MAX_STEPS = 8

outbox: ContextVar[list[bytes] | None] = ContextVar("image_outbox", default=None)


def build_image_tools() -> list:
    if not (settings.cloudflare_account_id and settings.cloudflare_api_token):
        log.info("CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN not set — generate_image disabled.")
        return []

    @tool
    async def generate_image(prompt: str, steps: int = 4) -> str:
        """Create an image from a text prompt and send it to the user with your
        reply. Write a detailed, concrete visual prompt in English (subject,
        style, composition, lighting). `steps` is 1-8: more is slightly better
        and uses more of the daily free quota; keep the default unless asked.
        Returns a status line, not the image; don't invent a link or path."""
        box = outbox.get()
        if box is None:
            return "IMAGE_ERROR: no reply to attach the image to."
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{settings.cloudflare_account_id}/ai/run/{_MODEL}"
        )
        headers = {"Authorization": f"Bearer {settings.cloudflare_api_token}"}
        body = {"prompt": prompt, "steps": max(1, min(steps, _MAX_STEPS))}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = base64.b64decode(resp.json()["result"]["image"])
        except Exception as exc:  # noqa: BLE001 - surface a usable message to the model
            log.warning("Cloudflare image generation failed: %s", exc)
            return f"IMAGE_ERROR: {exc}"
        box.append(data)
        return "Image generated; it will be attached to your reply."

    return [generate_image]
