"""Discord front-end (discord.py, pure Python — no Node).

Responds to allowlisted users in DMs, and in servers only when @mentioned.
Requires the privileged "Message Content Intent" enabled in the Discord
Developer Portal (see README).
"""
from __future__ import annotations

import asyncio
import logging

import discord

from app import threads
from app.config import settings
from app.messaging.dispatch import chunk, handle_turn

log = logging.getLogger(__name__)

PLATFORM = "discord"
_LIMIT = 2000


class _Client(discord.Client):
    def __init__(self, ctx, **kwargs):
        super().__init__(**kwargs)
        self.ctx = ctx

    async def on_ready(self) -> None:
        log.info(
            "Discord adapter online as %s. Authorized: %s",
            self.user,
            sorted(settings.allowed_discord_user_ids),
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id == self.user.id:
            return

        uid = message.author.id
        authorized = uid in settings.allowed_discord_user_ids
        content = (message.content or "").strip()

        if content == "!whoami":
            await message.channel.send(
                f"platform=discord user_id={uid} authorized={authorized}"
            )
            return
        if not authorized:
            return

        is_dm = message.guild is None
        mentioned = self.user in message.mentions
        if not is_dm and not mentioned:
            return
        if mentioned:  # strip the leading mention so the agent sees clean text
            content = content.replace(self.user.mention, "").strip()

        if content == "!help":
            await message.channel.send(
                "I search the web, run code in a sandbox, and remember things "
                "across conversations. In servers, @mention me. Commands: "
                "!reset (fresh conversation), !whoami."
            )
            return
        if content == "!reset":
            await threads.reset(self.ctx.pool, PLATFORM, message.channel.id)
            await message.channel.send("Fresh conversation started.")
            return
        if not content:
            return

        try:
            async with message.channel.typing():
                reply = await handle_turn(
                    self.ctx, PLATFORM, uid, message.channel.id, content
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("agent failed")
            await message.channel.send(f"Something went wrong: {exc}")
            return
        for part in chunk(reply, _LIMIT):
            await message.channel.send(part)


async def start(ctx):
    intents = discord.Intents.default()
    intents.message_content = True
    client = _Client(ctx, intents=intents)
    task = asyncio.create_task(client.start(settings.discord_bot_token))
    return client, task


async def stop(handle) -> None:
    client, task = handle
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        log.exception("discord shutdown error")
    task.cancel()
