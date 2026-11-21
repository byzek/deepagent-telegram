"""Send files and voice notes to the user alongside the reply.

Like generated images (see `tools.image`), attachments never enter the
conversation: `handle_turn` opens an `outbox` for each turn, these tools append
to it, and the messaging adapter uploads whatever is in it. The bot token stays
in the harness; the model (and anything running in the sandbox) never sees it.
"""
from __future__ import annotations

import logging
import posixpath
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from langchain_core.tools import tool

from app import voice
from app.config import settings

log = logging.getLogger(__name__)

KINDS = ("auto", "document", "photo", "voice", "audio", "video")
_BY_EXT = {
    "voice": {".ogg", ".oga", ".opus"},
    "audio": {".mp3", ".m4a", ".wav", ".flac", ".aac"},
    "photo": {".jpg", ".jpeg", ".png", ".webp"},
    "video": {".mp4", ".mov", ".webm"},
}
_PHOTO_MAX = 10 * 1024 * 1024  # larger photos must go as documents


@dataclass
class Attachment:
    kind: str  # one of KINDS minus "auto"
    data: bytes
    filename: str
    caption: str = ""


outbox: ContextVar[list[Attachment] | None] = ContextVar("file_outbox", default=None)


def guess_kind(filename: str, size: int) -> str:
    ext = posixpath.splitext(filename.lower())[1]
    kind = next((k for k, exts in _BY_EXT.items() if ext in exts), "document")
    return "document" if kind == "photo" and size > _PHOTO_MAX else kind


async def read_sandbox_file(path: str) -> bytes:
    async with httpx.AsyncClient(timeout=settings.sandbox_timeout + 15) as client:
        r = await client.post(
            f"{settings.sandbox_url}/read",
            json={"path": path},
            headers={"X-Sandbox-Token": settings.sandbox_token},
        )
        if r.status_code != 200:
            raise RuntimeError(r.json().get("error", f"HTTP {r.status_code}"))
        return r.content


def build_file_tools() -> list:
    @tool
    async def send_file(path: str, kind: str = "auto", caption: str = "") -> str:
        """Send a file from the sandbox workspace (e.g. /workspace/report.pdf)
        to the user as an attachment with your reply. `kind` is auto, document,
        photo, voice, audio or video; auto picks from the extension (.ogg/.opus
        become voice notes, .mp3/.m4a audio, images photos, otherwise a
        document). Voice notes must be OGG/Opus. Max 50 MB. Returns a status
        line; don't invent links."""
        box = outbox.get()
        if box is None:
            return "FILE_ERROR: no reply to attach the file to."
        if kind not in KINDS:
            return f"FILE_ERROR: kind must be one of {', '.join(KINDS)}."
        try:
            data = await read_sandbox_file(path)
        except Exception as exc:  # noqa: BLE001 - surface a usable message to the model
            return f"FILE_ERROR: {exc}"
        name = posixpath.basename(path) or "file"
        box.append(Attachment(guess_kind(name, len(data)) if kind == "auto" else kind,
                              data, name, caption))
        return f"{name} ({len(data)} bytes) will be attached to your reply."

    tools = [send_file]
    if voice.enabled():
        @tool
        async def send_voice_message(text: str, speed: float = 1.0) -> str:
            """Speak `text` aloud (text-to-speech) and send it to the user as a
            voice note with your reply. Plain spoken prose only, no Markdown.
            `speed` 0.5-2.0. Your normal text reply is still sent too."""
            box = outbox.get()
            if box is None:
                return "VOICE_ERROR: no reply to attach the voice note to."
            spoken = voice.speakable(text)
            if not spoken:
                return "VOICE_ERROR: nothing speakable in the text."
            try:
                data = await voice.synthesize(spoken, max(0.5, min(speed, 2.0)))
            except Exception as exc:  # noqa: BLE001
                log.warning("TTS failed: %s", exc)
                return f"VOICE_ERROR: {exc}"
            box.append(Attachment("voice", data, "voice.ogg"))
            return "Voice note will be attached to your reply."

        tools.append(send_voice_message)
    return tools
