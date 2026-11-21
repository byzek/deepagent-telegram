"""Local speech service: Whisper STT + Piper TTS behind a token-gated HTTP API.

POST /stt   body = any audio ffmpeg can decode (Telegram sends OGG/Opus)
            -> {"text": "...", "language": "ru"}
POST /tts   body = {"text": "...", "speed": 1.0}
            -> audio/ogg (Opus), ready for Telegram's sendVoice
GET  /healthz

Mixed Russian/English text is split by script and each run is spoken by the
matching Piper voice, so "Скажи 'hedging my bets' ещё раз" sounds right.
Only callers presenting `X-Voice-Token` are served.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("voice")

TOKEN = os.environ.get("VOICE_TOKEN", "")
PIPER_DIR = os.environ.get("PIPER_DIR", "/opt/piper")
VOICES = {
    "ru": os.environ.get("TTS_VOICE_RU", "ru_RU-irina-medium"),
    "en": os.environ.get("TTS_VOICE_EN", "en_US-amy-medium"),
}
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
# Biases Whisper toward keeping code-switched English as English instead of
# transliterating it into Cyrillic.
WHISPER_PROMPT = os.environ.get(
    "WHISPER_PROMPT", "Разговор на русском, иногда по-английски. Wait, how do I say that?"
)
MAX_BODY = 25 * 1024 * 1024
SAMPLE_RATE = 16000

_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-z]")


def split_by_script(text: str, default: str = "ru") -> list[tuple[str, str]]:
    """Split text into (lang, chunk) runs: Cyrillic -> ru, Latin -> en.
    Digits/punctuation/spaces stay with the run they're in, so numbers are
    read in the surrounding language."""
    runs: list[tuple[str, str]] = []
    lang, buf = None, []
    for ch in text:
        cur = "ru" if _CYR.match(ch) else "en" if _LAT.match(ch) else None
        if cur and lang and cur != lang:
            runs.append((lang, "".join(buf)))
            buf = []
        if cur:
            lang = cur
        buf.append(ch)
    if buf:
        runs.append((lang or default, "".join(buf)))
    return [(lg, s.strip()) for lg, s in runs if s.strip()]


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        input=data, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-500:])
    return proc.stdout


class Engines:
    """Lazy-loaded models; one lock each since neither is safe to share
    across threads mid-inference."""

    def __init__(self):
        self._whisper = None
        self._voices: dict = {}
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()

    def transcribe(self, audio: bytes) -> dict:
        import numpy as np

        pcm = _ffmpeg(["-i", "pipe:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"], audio)
        samples = np.frombuffer(pcm, dtype=np.float32)
        with self._stt_lock:
            if self._whisper is None:
                from faster_whisper import WhisperModel

                log.info("loading whisper %s (%s/%s)", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
                self._whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
            segments, info = self._whisper.transcribe(
                samples, beam_size=5, vad_filter=True, initial_prompt=WHISPER_PROMPT
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return {"text": text, "language": info.language}

    def _voice(self, lang: str):
        if lang not in self._voices:
            from piper import PiperVoice

            self._voices[lang] = PiperVoice.load(os.path.join(PIPER_DIR, VOICES[lang] + ".onnx"))
        return self._voices[lang]

    def synthesize(self, text: str, speed: float = 1.0) -> bytes:
        import numpy as np
        from piper import SynthesisConfig

        speed = min(max(speed, 0.5), 2.0)
        cfg = SynthesisConfig(length_scale=1.0 / speed)
        out_rate, parts = None, []
        with self._tts_lock:
            for lang, chunk in split_by_script(text):
                for c in self._voice(lang).synthesize(chunk, syn_config=cfg):
                    audio = c.audio_float_array.astype(np.float32)
                    if out_rate is None:
                        out_rate = c.sample_rate
                    elif c.sample_rate != out_rate:  # voices of different quality tiers
                        n = int(len(audio) * out_rate / c.sample_rate)
                        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
                    parts.append(audio)
                    parts.append(np.zeros(int(out_rate * 0.12), dtype=np.float32))
        if not parts:
            raise ValueError("nothing speakable in text")
        pcm = np.concatenate(parts).tobytes()
        return _ffmpeg(
            ["-f", "f32le", "-ar", str(out_rate), "-ac", "1", "-i", "pipe:0",
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1"],
            pcm,
        )


ENGINES = Engines()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._reply(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not TOKEN or not hmac.compare_digest(self.headers.get("X-Voice-Token", ""), TOKEN):
            return self._json(401, {"error": "unauthorized"})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            return self._json(413, {"error": "empty or too large"})
        body = self.rfile.read(size)
        try:
            if self.path == "/stt":
                return self._json(200, ENGINES.transcribe(body))
            if self.path == "/tts":
                req = json.loads(body)
                audio = ENGINES.synthesize(str(req.get("text", "")), float(req.get("speed", 1.0)))
                return self._reply(200, audio, "audio/ogg")
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("%s failed", self.path)
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    if not TOKEN:
        raise SystemExit("VOICE_TOKEN is required")
    if os.environ.get("VOICE_PRELOAD", "true").lower() in ("1", "true", "yes"):
        threading.Thread(target=lambda: ENGINES.transcribe(_silence()), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


def _silence() -> bytes:
    """One second of silent WAV, used to warm the Whisper model at boot."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"\0\0" * SAMPLE_RATE)
    return buf.getvalue()


if __name__ == "__main__":
    main()
