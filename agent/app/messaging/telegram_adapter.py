"""Telegram front-end (long-polling), started non-blocking so it can run
alongside other adapters in one event loop."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
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


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        log.info("Ignoring Telegram message from %s", update.effective_user)
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    ctx = _ctx(context)
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = await handle_turn(ctx, PLATFORM, update.effective_user.id, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent failed")
        await update.message.reply_text(f"Something went wrong: {exc}")
        return
    for part in chunk(reply, _LIMIT):
        await update.message.reply_text(part)


def _build(ctx) -> Application:
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
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
