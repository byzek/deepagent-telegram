# syntax=docker/dockerfile:1

# ---- builder: resolve + install deps with uv into a self-contained venv ------
FROM python:3.12-slim AS builder

# uv: fast, reproducible Python package manager (Astral). No Node toolchain.
# Recommend pinning to a digest in production; `latest` used here so the build
# never breaks on a bad tag.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN uv venv /opt/venv

WORKDIR /build
COPY requirements.txt .
# Cache mount keeps rebuilds fast when requirements don't change.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -r requirements.txt

# ---- runtime: minimal image, non-root ----------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ ./app/
COPY seed/ ./seed/

USER appuser

# Long-polling / gateway clients: no inbound ports. Just run the process.
CMD ["python", "-m", "app.main"]
