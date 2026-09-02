# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A few commonly useful CLIs for the agent to work with. Add more as needed,
# then rebuild. Kept intentionally small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user; own /workspace so the (initially empty) volume inherits
# writable ownership on first mount.
RUN useradd --create-home --uid 10002 sandboxer \
    && mkdir -p /workspace \
    && chown sandboxer:sandboxer /workspace

COPY exec_server.py /app/exec_server.py

USER sandboxer
WORKDIR /workspace

CMD ["python", "/app/exec_server.py"]
