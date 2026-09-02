"""Entrypoint: build the runtime, then start every enabled messaging adapter
plus the background network sampler in one event loop."""
from __future__ import annotations

import asyncio
import logging
import signal

from app.config import settings
from app.core import build_app_context
from app.metrics import network_sampler


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


async def amain() -> None:
    log = logging.getLogger("app.main")
    ctx = await build_app_context()

    sampler = asyncio.create_task(
        network_sampler(
            ctx.pool,
            settings.net_sample_interval,
            settings.net_metrics_retention_hours,
        )
    )

    started = []  # (module, handle) pairs
    platforms = settings.enabled_platforms()
    if "telegram" in platforms:
        from app.messaging import telegram_adapter

        started.append((telegram_adapter, await telegram_adapter.start(ctx)))
    if "discord" in platforms:
        from app.messaging import discord_adapter

        started.append((discord_adapter, await discord_adapter.start(ctx)))

    log.info("Started adapters: %s", platforms)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    await stop_event.wait()
    log.info("Shutting down…")

    sampler.cancel()
    for module, handle in started:
        await module.stop(handle)
    await ctx.close()


def main() -> None:
    _configure_logging()
    settings.validate()
    asyncio.run(amain())


if __name__ == "__main__":
    main()
