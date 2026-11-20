"""Entrypoint: build the runtime, then start every enabled messaging adapter
plus the background network sampler in one event loop."""
from __future__ import annotations

import asyncio
import functools
import logging
import signal

from app.config import settings
from app.catalog import catalog_sampler
from app.core import build_app_context, seed_user_ids
from app.skills_install import _default_run_cmd
from app.metrics import network_sampler
from app.netconns import connection_sampler
from app.scheduler import scheduler_loop


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

    run_cmd = lambda c: _default_run_cmd(c, settings.sandbox_url, settings.sandbox_token)  # noqa: E731
    conns = asyncio.create_task(
        connection_sampler(
            ctx.pool, run_cmd, settings.net_sample_interval, settings.net_metrics_retention_hours
        )
    )
    cataloger = asyncio.create_task(
        catalog_sampler(
            ctx.pool,
            ctx.store,
            seed_user_ids(),
            ctx.agent.baked_tools,
            run_cmd,
            settings.catalog_interval,
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

    senders = {
        module.PLATFORM: functools.partial(module.send, handle)
        for module, handle in started
    }
    sched = asyncio.create_task(scheduler_loop(ctx, senders))

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
    cataloger.cancel()
    conns.cancel()
    sched.cancel()
    for module, handle in started:
        await module.stop(handle)
    await ctx.close()


def main() -> None:
    _configure_logging()
    settings.validate()
    asyncio.run(amain())


if __name__ == "__main__":
    main()
