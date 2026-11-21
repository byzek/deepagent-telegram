"""Client for the optional `voice` service (Whisper STT + Piper TTS)."""
from __future__ import annotations

import re

import httpx

from app.config import settings


def enabled() -> bool:
    return bool(settings.voice_url)


def _headers() -> dict:
    return {"X-Voice-Token": settings.voice_token}


async def transcribe(audio: bytes) -> str:
    async with httpx.AsyncClient(timeout=settings.voice_timeout) as client:
        r = await client.post(f"{settings.voice_url}/stt", content=audio, headers=_headers())
        r.raise_for_status()
        return r.json().get("text", "").strip()


async def synthesize(text: str, speed: float = 1.0) -> bytes:
    async with httpx.AsyncClient(timeout=settings.voice_timeout) as client:
        r = await client.post(
            f"{settings.voice_url}/tts", json={"text": text, "speed": speed}, headers=_headers()
        )
        r.raise_for_status()
        return r.content


_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_`#>|~]+")
_BULLET = re.compile(r"^\s*(?:[-+]|\d+[.)])\s+", re.M)


def speakable(text: str) -> str:
    """Strip Markdown, code, and URLs so TTS doesn't read out syntax."""
    text = _FENCE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _BULLET.sub("", text)
    text = _MARKS.sub("", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()
