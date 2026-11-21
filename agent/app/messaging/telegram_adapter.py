"""Telegram front-end (long-polling), started non-blocking so it can run
alongside other adapters in one event loop."""
from __future__ import annotations

import asyncio
import functools
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

from app import practice, russian, threads, voice
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
# Longest side of the photo variant sent to the model. Telegram already keeps
# downscaled copies (~1280 is the usual largest), so no local resizing needed.
# Each image is also stored in the thread, so smaller keeps history cheaper.
_PHOTO_MAX_SIDE = 1280


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
        "/deliberate <question> — best-of-N answer for hard questions "
        "(or start any message with the word \"deliberate\")\n"
        "/practice on|off|now|status — nightly skill drills\n"
        "/russian on|off|now|stop|status — spoken Russian practice (voice notes)\n"
        "Send a voice note any time and I'll answer out loud.\n"
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


async def _practice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    msg = await practice.command(
        _ctx(context).pool, PLATFORM, str(update.effective_user.id),
        str(update.effective_chat.id), " ".join(context.args or []),
    )
    await update.message.reply_text(msg)


async def _russian(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not voice.enabled():
        await update.message.reply_text(
            "Russian practice needs the voice service (set VOICE_URL and start the 'voice' profile)."
        )
        return
    msg = await russian.command(
        _ctx(context).pool, PLATFORM, str(update.effective_user.id),
        str(update.effective_chat.id), " ".join(context.args or []),
    )
    await update.message.reply_text(msg)


async def _send(target, text: str, *, parse_mode=None) -> None:
    """Send one message, retrying transient Telegram network hiccups instead of
    letting a flaky send bubble up as a failed turn. `target` is a Message to
    reply to, or a send coroutine (e.g. bot.send_message bound to a chat)."""
    send = target.reply_text if isinstance(target, Message) else target
    for attempt in range(3):
        try:
            await send(text, parse_mode=parse_mode)
            return
        except RetryAfter as exc:
            await asyncio.sleep(getattr(exc, "retry_after", 1) + 0.5)
        except (TimedOut, NetworkError):
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))


async def _reply_markdown(message, text: str) -> None:
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


async def _keep_typing(bot, chat_id: int, stop: asyncio.Event,
                       action: str = ChatAction.TYPING) -> None:
    """Refresh the 'typing…' indicator until stopped, so long turns don't look
    dead while the model is still generating."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
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


async def _speak(send_voice, reply) -> bool:
    """Send the reply as a voice note. False if it couldn't (caller sends text)."""
    spoken = voice.speakable(reply.text)
    if not spoken or len(spoken) > settings.voice_max_chars:
        return False
    try:
        await send_voice(voice=await voice.synthesize(spoken, reply.speed))
        return True
    except Exception:  # noqa: BLE001 - fall back to text rather than lose the reply
        log.exception("TTS failed; sending text instead")
        return False


async def _deliver(message, send_photo, reply, send_voice=None) -> None:
    """Send the reply (as a voice note when requested and possible, plus the
    text unless transcripts are off), then each generated image as a photo."""
    spoke = bool(reply.voice and send_voice and voice.enabled() and await _speak(send_voice, reply))
    if (reply.text or not reply.images) and not (spoke and not settings.voice_transcripts):
        await _reply_markdown(message, reply.text)
    for data in reply.images:
        await send_photo(photo=data)


def pick_photo(sizes, max_side: int = _PHOTO_MAX_SIDE):
    """The largest variant whose longest side fits `max_side`, else the
    smallest one available."""
    fits = [s for s in sizes if max(s.width, s.height) <= max_side]
    if fits:
        return max(fits, key=lambda s: s.width * s.height)
    return min(sizes, key=lambda s: s.width * s.height)


async def _run_turn(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    text: str, images=(), *, spoken: bool = False) -> None:
    ctx = _ctx(context)
    chat_id = update.effective_chat.id
    # An active Russian session takes over the chat: its own thread, spoken replies.
    thread = await russian.route(ctx.pool, PLATFORM, chat_id) if voice.enabled() else None
    if thread:
        text = russian.user_turn(text, voice=spoken)
    talk = spoken or bool(thread)
    stop = asyncio.Event()
    action = ChatAction.RECORD_VOICE if talk else ChatAction.TYPING
    typing = asyncio.create_task(_keep_typing(context.bot, chat_id, stop, action))
    try:
        reply = await handle_turn(
            ctx, PLATFORM, update.effective_user.id, chat_id, text, images=images, thread=thread
        )
        reply.voice = talk
        reply.speed = settings.russian_speed if thread else 1.0
    except Exception as exc:  # noqa: BLE001
        log.exception("agent failed")
        await _send(update.message, _friendly_error(exc))
        return
    finally:
        stop.set()
        await typing

    await _deliver(update.message, update.message.reply_photo, reply, update.message.reply_voice)


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        log.info("Ignoring Telegram message from %s", update.effective_user)
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _run_turn(update, context, text)


async def _on_deliberate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Commands skip _on_message; pass the full "/deliberate ..." text through.
    if not _authorized(update):
        return
    await _run_turn(update, context, (update.message.text or "").strip())


async def _on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        log.info("Ignoring Telegram photo from %s", update.effective_user)
        return
    if not settings.llm_vision:
        await _send(update.message, "I can't see images right now (LLM_VISION is off).")
        return
    size = pick_photo(update.message.photo)
    file = await context.bot.get_file(size.file_id)
    data = bytes(await file.download_as_bytearray())
    await _run_turn(update, context, (update.message.caption or "").strip(), [data])


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        log.info("Ignoring Telegram voice note from %s", update.effective_user)
        return
    if not voice.enabled():
        await _send(update.message, "I can't listen to voice notes right now (VOICE_URL is not set).")
        return
    media = update.message.voice or update.message.audio
    file = await context.bot.get_file(media.file_id)
    data = bytes(await file.download_as_bytearray())
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        text = await voice.transcribe(data)
    except Exception as exc:  # noqa: BLE001
        log.exception("transcription failed")
        await _send(update.message, f"I couldn't transcribe that: {exc}")
        return
    if not text:
        await _send(update.message, "I couldn't hear anything in that voice note. Try again?")
        return
    await _run_turn(update, context, text, spoken=True)


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
    app.add_handler(CommandHandler("practice", _practice))
    app.add_handler(CommandHandler("russian", _russian))
    app.add_handler(CommandHandler("deliberate", _on_deliberate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))
    return app


async def start(ctx) -> Application:
    app = _build(ctx)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Telegram adapter polling. Authorized: %s",
             sorted(settings.allowed_telegram_user_ids))
    return app


async def send(app: Application, chat_id, reply) -> None:
    """Deliver an unprompted reply (e.g. a scheduled task's result)."""
    await _deliver(
        functools.partial(app.bot.send_message, int(chat_id)),
        functools.partial(app.bot.send_photo, int(chat_id)),
        reply,
        functools.partial(app.bot.send_voice, int(chat_id)),
    )


async def stop(app: Application) -> None:
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:  # noqa: BLE001
        log.exception("telegram shutdown error")
