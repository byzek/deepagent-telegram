# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/models/hf \
    PIPER_DIR=/opt/piper

# ffmpeg decodes Telegram's OGG/Opus voice notes and encodes replies back to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN uv venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -r /tmp/requirements.txt

# Bake the Piper voices into the image (~60 MB each). Override at build time:
#   docker compose build --build-arg PIPER_VOICES="ru_RU-dmitri-medium en_US-lessac-medium" voice
ARG PIPER_VOICES="ru_RU-irina-medium en_US-amy-medium"
RUN mkdir -p $PIPER_DIR && python -m piper.download_voices --download-dir $PIPER_DIR $PIPER_VOICES

RUN useradd --create-home --uid 10003 voice && mkdir -p /models && chown voice:voice /models
COPY voice_server.py /app/voice_server.py

USER voice
WORKDIR /app
CMD ["python", "/app/voice_server.py"]
