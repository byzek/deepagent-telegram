"""Telegram front-end (long-polling), started non-blocking so it can run
alongside other adapters in one event loop."""
from __future__ import annotations

import asyncio
import logging

import telegramify_markdown
from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import threads
from app.config import settings
from app.messaging.dispatch import chunk, handle_turn

log = logging.getLogger(__name__)

PLATFORM = "telegram"
_LIMIT = 4096
# Convert originals in sub-limit slices: MarkdownV2 escaping inflates length, so
# leave headroom below the 4096 hard cap so a converted chunk still fits.
_CHUNK = 3500
# Telegram's "typing…" indicator lasts ~5s; refresh a bit faster than that.
_TYPING_REFRESH = 4.0


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in settings.allowed_telegram_user_ids)


def _ctx(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["ctx"]


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        f"Hi — I'm {settings.agent_name}. Send me anything. "
        "Commands: /reset, /whoami, /help."
    )


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "I search the web, run code in a sandbox, and remember things about you "
        "across conversations (with semantic recall).\n\n"
        "/reset — fresh conversation (keeps long-term memory)\n"
        "/whoami — your Telegram user id\n"
        "/help — this message"
    )


async def _whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"platform=telegram user_id={update.effective_user.id} "
        f"authorized={_authorized(update)}"
    )


async def _reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await threads.reset(_ctx(context).pool, PLATFORM, update.effective_chat.id)
    await update.message.reply_text("Fresh conversation started.")


async def _send(message: Message, text: str, *, parse_mode=None) -> None:
    """Send one message, retrying transient Telegram network hiccups instead of
    letting a flaky send bubble up as a failed turn."""
    for attempt in range(3):
        try:
            await message.reply_text(text, parse_mode=parse_mode)
            return
        except RetryAfter as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 1) + 0.5)
        except (TimedOut, NetworkError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))


async def _reply_markdown(message: Message, text: str) -> None:
    """Render the agent's Markdown as Telegram MarkdownV2, converting per chunk
    so each message is self-contained. If Telegram rejects the entities (or the
    message is too long), fall back to sending that chunk as plain text."""
    for raw in chunk(text, _CHUNK):
        try:
            md = telegramify_markdown.markdownify(raw)
            await _send(message, md, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            log.warning("MarkdownV2 rejected by Telegram; sending plain text")
            await _send(message, raw)


async def _keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    """Refresh the 'typing…' indicator until stopped, so long turns don't look
    dead while the model is still generating."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:  # noqa: BLE001 - the indicator is best-effort
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TYPING_REFRESH)
        except asyncio.TimeoutError:
            pass


def _friendly_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name or "timed out" in str(exc).lower():
        return (
            "The model went quiet for too long (or hit the hard time ceiling), so "
            "I stopped waiting. Try again, or send a shorter request. (Admins: "
            "tune LLM_STREAM_IDLE_TIMEOUT / LLM_HARD_TIMEOUT.)"
        )
    return f"Something went wrong: {exc}"


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        log.info("Ignoring Telegram message from %s", update.effective_user)
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    ctx = _ctx(context)
    chat_id = update.effective_chat.id
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(context.bot, chat_id, stop))
    try:
        reply = await handle_turn(ctx, PLATFORM, update.effective_user.id, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent failed")
        await _send(update.message, _friendly_error(exc))
        return
    finally:
        stop.set()
        await typing

    await _reply_markdown(update.message, reply)


def _build(ctx) -> Application:
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        # Generous HTTP timeouts so sending replies (or a slow Bot API) doesn't
        # surface as a failed turn under load.
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.bot_data["ctx"] = ctx
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("whoami", _whoami))
    app.add_handler(CommandHandler("reset", _reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    return app


async def start(ctx) -> Application:
    app = _build(ctx)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Telegram adapter polling. Authorized: %s",
             sorted(settings.allowed_telegram_user_ids))
    return app


async def stop(app: Application) -> None:
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:  # noqa: BLE001
        log.exception("telegram shutdown error")
