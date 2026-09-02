"""Metrics collection: per-turn LLM stats + agent-container network sampling.

Everything is written to Postgres so the separate `dashboard` service can read
it without any privileged access (no Docker socket, no /proc of other
containers). See the dashboard for visualization.
"""
from __future__ import annotations

import asyncio
import logging
import time

from langchain_core.callbacks import AsyncCallbackHandler

log = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS metrics_llm (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id         TEXT NOT NULL,
    platform        TEXT,
    memory_backend  TEXT,
    model           TEXT,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    llm_seconds     DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens_per_sec  DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS metrics_llm_ts_idx ON metrics_llm (ts);
CREATE INDEX IF NOT EXISTS metrics_llm_user_idx ON metrics_llm (user_id);

CREATE TABLE IF NOT EXISTS metrics_net (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    iface       TEXT NOT NULL,
    rx_bytes    BIGINT NOT NULL,
    tx_bytes    BIGINT NOT NULL,
    rx_packets  BIGINT NOT NULL,
    tx_packets  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS metrics_net_ts_idx ON metrics_net (ts);
"""


async def ensure_metrics_tables(pool) -> None:
    # The pool runs with prepare_threshold=0, so every statement goes through the
    # extended/prepared protocol — which allows only ONE command per execute().
    # Run each DDL statement separately instead of the whole script at once.
    async with pool.connection() as conn:
        for stmt in filter(None, (s.strip() for s in DDL.split(";"))):
            await conn.execute(stmt)
    log.info("Metrics tables ready.")


def _extract_tokens(response) -> tuple[int, int, str | None]:
    """Return (prompt_tokens, completion_tokens, model_name) from an LLMResult."""
    prompt = completion = 0
    model = None
    llm_output = getattr(response, "llm_output", None) or {}
    usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
    if usage:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
    model = llm_output.get("model_name") or llm_output.get("model")

    if not (prompt or completion):
        for gens in getattr(response, "generations", []) or []:
            for gen in gens:
                msg = getattr(gen, "message", None)
                um = getattr(msg, "usage_metadata", None) if msg else None
                if um:
                    prompt += int(um.get("input_tokens", 0) or 0)
                    completion += int(um.get("output_tokens", 0) or 0)
                if model is None and msg is not None:
                    meta = getattr(msg, "response_metadata", {}) or {}
                    model = meta.get("model_name") or meta.get("model")
    return prompt, completion, model


class TurnMetrics(AsyncCallbackHandler):
    """One instance per user turn. Accumulates pure LLM generation time + tokens
    across every model call the agent makes (main agent, tool loop, subagents)."""

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.llm_seconds = 0.0
        self.calls = 0
        self.model: str | None = None

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        self._starts[str(run_id)] = time.monotonic()

    async def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        self._starts[str(run_id)] = time.monotonic()

    async def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        start = self._starts.pop(str(run_id), None)
        if start is not None:
            self.llm_seconds += time.monotonic() - start
        self.calls += 1
        prompt, completion, model = _extract_tokens(response)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        if model:
            self.model = model

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_per_sec(self) -> float:
        return self.completion_tokens / self.llm_seconds if self.llm_seconds > 0 else 0.0


async def write_turn(pool, *, user_id: str, platform: str, memory_backend: str,
                     model: str | None, m: TurnMetrics) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO metrics_llm
              (user_id, platform, memory_backend, model, prompt_tokens,
               completion_tokens, total_tokens, llm_calls, llm_seconds, tokens_per_sec)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                user_id, platform, memory_backend, m.model or model,
                m.prompt_tokens, m.completion_tokens, m.total_tokens,
                m.calls, m.llm_seconds, m.tokens_per_sec,
            ),
        )


def _read_proc_net_dev() -> tuple[int, int, int, int]:
    """Return cumulative (rx_bytes, tx_bytes, rx_packets, tx_packets) summed over
    all interfaces except loopback."""
    rx_b = tx_b = rx_p = tx_p = 0
    try:
        with open("/proc/net/dev", "r") as fh:
            for line in fh.readlines()[2:]:
                iface, _, rest = line.partition(":")
                iface = iface.strip()
                if iface in ("lo", ""):
                    continue
                f = rest.split()
                if len(f) < 16:
                    continue
                rx_b += int(f[0]); rx_p += int(f[1])
                tx_b += int(f[8]); tx_p += int(f[9])
    except FileNotFoundError:
        pass  # non-Linux host; network panel will simply have no data
    return rx_b, tx_b, rx_p, tx_p


async def network_sampler(pool, interval: float, retention_hours: int) -> None:
    """Background loop: snapshot cumulative counters into metrics_net."""
    log.info("Network sampler started (every %.0fs).", interval)
    prune_every = max(1, int(300 / interval))  # prune roughly every 5 min
    tick = 0
    while True:
        try:
            rx_b, tx_b, rx_p, tx_p = _read_proc_net_dev()
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO metrics_net (iface, rx_bytes, tx_bytes, rx_packets, tx_packets)"
                    " VALUES ('total',%s,%s,%s,%s)",
                    (rx_b, tx_b, rx_p, tx_p),
                )
                tick += 1
                if tick % prune_every == 0:
                    await conn.execute(
                        "DELETE FROM metrics_net WHERE ts < now() - make_interval(hours => %s)",
                        (retention_hours,),
                    )
        except Exception:  # noqa: BLE001 - never let the sampler kill the app
            log.exception("network sampler error")
        await asyncio.sleep(interval)
