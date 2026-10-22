"""`pay-my-bills`: the Telegram bot that polls Gmail and asks for the money.

Long polling, so nothing listens on a port; it runs wherever this machine is
on. Every `GMAIL_POLL_SECONDS` it polls Gmail (`service.poll`) and sends
what's waiting (`service.outbox`) to every allowed user. A request comes with
a **Request on Venmo** button (the prefilled link) and **Mark requested** /
**Skip** buttons; unmarked requests are re-sent once a day, up to three times.

Commands: /status, /check (poll now), /backfill <days> (read older mail).
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from pay_my_bills import service
from pay_my_bills.config import BotSettings, GmailSettings, Settings
from pay_my_bills.db import make_session_factory, migrate, utcnow
from pay_my_bills.gmail import AuthenticationRequired, Gmail

SEND_RETRY_S = 120
SEND_BACKOFF_MAX_S = 30
MAX_BACKFILL_DAYS = 365

HELP = (
    "I watch Gmail for Ventura Water and SCE autopay emails and send you a "
    "ready-made Venmo request for your roommate's share.\n\n"
    "/status: what's open, with buttons to mark each one paid or skip it\n"
    "/check: look at Gmail now\n"
    "/backfill 45: read the last 45 days of bill emails"
)


class BillsBot:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        mailbox: Callable[[], service.Mailbox],
        settings: Settings,
        bot_settings: BotSettings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.mailbox = mailbox
        self.settings = settings
        self.bot_settings = bot_settings
        self.clock = clock
        # /check and the timer mustn't poll at once and fetch the same mail.
        self._lock = asyncio.Lock()
        self._last_error: str | None = None
        # The last problem the person was actually told about.
        self._reported_error: str | None = None
        self._poller: asyncio.Task | None = None

    def install(self, application: Application) -> None:
        # Group -1 runs before every other handler; raising stops the update.
        application.add_handler(TypeHandler(Update, self._gate), group=-1)
        application.add_handler(CommandHandler(["start", "help"], self._help))
        application.add_handler(CommandHandler("status", self._status))
        application.add_handler(CommandHandler("check", self._check))
        application.add_handler(CommandHandler("backfill", self._backfill))
        application.add_handler(CallbackQueryHandler(self._on_button))

    # --- handlers ---------------------------------------------------------

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.bot_settings.allowed_user_ids:
            logger.warning(
                f"Ignored an update from Telegram user {user.id if user else '?'}"
            )
            raise ApplicationHandlerStop

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP)

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = await self._status_message()
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )

    async def _status_message(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """What's open, with Paid / Skip for each request, so anything can be
        closed from here rather than by finding its own message."""
        text = await asyncio.to_thread(service.status, self.sessions, self.settings)
        labels = await asyncio.to_thread(service.open_request_labels, self.sessions)
        if not labels:
            return text, None
        return text, InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ {label} paid", callback_data=f"paid:{request_id}:status"
                    ),
                    InlineKeyboardButton(
                        "Skip", callback_data=f"skip:{request_id}:status"
                    ),
                ]
                for request_id, label in labels
            ]
        )

    async def _check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._poll_and_report(update, context.bot, service.DEFAULT_LOOKBACK_DAYS)

    async def _backfill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            days = int(context.args[0]) if context.args else 45
        except ValueError:
            days = 0
        if not 0 < days <= MAX_BACKFILL_DAYS:
            await update.effective_message.reply_text(
                f"Give a number of days from 1 to {MAX_BACKFILL_DAYS}, "
                "e.g. /backfill 45"
            )
            return
        await self._poll_and_report(update, context.bot, days)

    async def _poll_and_report(self, update: Update, bot: Any, days: int) -> None:
        found = await self.poll_once(bot, days)
        if found is None:
            await update.effective_message.reply_text(
                f"Couldn't check Gmail: {self._last_error}"
            )
        elif found == 0:
            await update.effective_message.reply_text(
                f"No new bill emails in the last {days} days."
            )
        else:
            await update.effective_message.reply_text(
                f"Read {found} new email{'s' if found != 1 else ''}."
            )

    async def _on_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        # "paid:3" from a request's own message, "paid:3:status" from /status.
        action, _, rest = (query.data or "").partition(":")
        raw_id, _, origin = rest.partition(":")
        if action not in ("done", "paid", "skip") or not raw_id.isdigit():
            await query.answer()
            return
        state = await asyncio.to_thread(
            service.mark, self.sessions, int(raw_id), action, self.clock()
        )
        await query.answer(state)
        message = query.message
        if origin == "status":
            # Redraw the list, so what was just closed drops out of it.
            text, markup = await self._status_message()
            try:
                await query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=markup
                )
            except BadRequest as e:
                logger.warning(f"Couldn't update the status message: {e}")
        elif message is not None and getattr(message, "text_html", None):
            try:
                await query.edit_message_text(
                    f"{message.text_html}\n\n<i>{state}</i>",
                    parse_mode=ParseMode.HTML,
                    # Requested isn't the end: keep a way to mark it paid
                    # (or give up on it) if Venmo's email never comes.
                    reply_markup=_open_buttons(int(raw_id))
                    if action == "done"
                    else None,
                )
            except BadRequest as e:
                logger.warning(f"Couldn't update the request message: {e}")

    # --- polling ----------------------------------------------------------

    def start_polling(self, bot: Any) -> None:
        self._poller = asyncio.create_task(self._poll_forever(bot))

    async def stop_polling(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)

    async def _poll_forever(self, bot: Any) -> None:
        while True:
            try:
                await self.poll_once(bot)
            except Exception:
                # Telegram unreachable while reporting a problem: try again
                # next time rather than stop polling for good.
                logger.exception("Poll failed")
            await asyncio.sleep(self.bot_settings.poll_seconds)

    async def poll_once(
        self, bot: Any, days: int = service.DEFAULT_LOOKBACK_DAYS
    ) -> int | None:
        """Poll Gmail, then send what's waiting. Returns how many new emails
        were read, or None when Gmail couldn't be read (the person is told,
        once per distinct problem)."""
        async with self._lock:
            found: int | None
            try:
                found = await asyncio.to_thread(
                    service.poll,
                    self.sessions,
                    self.mailbox,
                    self.settings,
                    self.clock(),
                    days,
                )
                self._last_error = self._reported_error = None
            except Exception as exc:
                found = None
                await self._problem(bot, exc)
                try:
                    await asyncio.to_thread(
                        service.schedule, self.sessions, self.settings, self.clock()
                    )
                except Exception:
                    logger.exception("Scheduling rent and autopays failed")
            # Even when Gmail is down: what's due (rent, a reminder) still goes.
            await self._deliver(bot)
            return found

    async def _problem(self, bot: Any, exc: Exception) -> None:
        if isinstance(exc, AuthenticationRequired):
            error = str(exc)
        else:
            logger.exception("Gmail poll failed")
            error = f"{type(exc).__name__}: {exc}"
        self._last_error = error
        if error == self._reported_error:
            return
        try:
            await self._broadcast(bot, f"⚠️ Couldn't check Gmail: {error}")
        except Exception:
            # Telegram down too: still schedule and deliver what's due, and
            # tell the person on a later poll.
            logger.exception("Reporting the Gmail problem failed")
            return
        self._reported_error = error

    async def _deliver(self, bot: Any) -> None:
        try:
            items = await asyncio.to_thread(
                service.outbox, self.sessions, self.settings, self.clock()
            )
        except Exception:
            logger.exception("Reading the outbox failed")
            return
        for item in items:
            markup = _request_buttons(item) if item.venmo_url else None
            try:
                await self._broadcast(bot, item.text, reply_markup=markup)
            except Exception:
                # Left unmarked, so the next poll tries again.
                logger.exception(f"Sending {item.kind} {item.ref} failed")
                continue
            await asyncio.to_thread(
                service.delivered, self.sessions, item, self.clock()
            )

    async def _broadcast(self, bot: Any, text: str, **kwargs: Any) -> None:
        for chat_id in sorted(self.bot_settings.allowed_user_ids):
            await send_with_retry(bot, chat_id, text, **kwargs)


def _request_buttons(item: service.Outgoing) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Request on Venmo", url=item.venmo_url)],
            [
                InlineKeyboardButton(
                    "✔️ Mark requested", callback_data=f"done:{item.ref}"
                ),
                InlineKeyboardButton("✅ Paid", callback_data=f"paid:{item.ref}"),
                InlineKeyboardButton("Skip", callback_data=f"skip:{item.ref}"),
            ],
        ]
    )


def _open_buttons(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Paid", callback_data=f"paid:{request_id}"),
                InlineKeyboardButton("Skip", callback_data=f"skip:{request_id}"),
            ]
        ]
    )


async def send_with_retry(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    """Send an HTML message, riding out a dropped connection or a rate limit.

    Gives up (raising) after `SEND_RETRY_S`. A BadRequest is a fault in the
    message itself; it's most likely the HTML, so the words go again plain.
    """
    try:
        await _retrying(
            chat_id,
            lambda: bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, **kwargs
            ),
        )
        return
    except BadRequest as e:
        logger.warning(f"HTML message to chat {chat_id} refused ({e}); sending plain")
    await _retrying(chat_id, lambda: bot.send_message(chat_id, text, **kwargs))


async def _retrying(chat_id: int, send: Callable[[], Awaitable[Any]]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SEND_RETRY_S
    backoff = 1.0
    while True:
        try:
            await send()
            return
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
        except BadRequest:
            raise
        except NetworkError as e:
            if loop.time() >= deadline:
                raise
            logger.warning(f"Sending to chat {chat_id} failed ({e}); retrying")
            wait, backoff = backoff, min(backoff * 2, SEND_BACKOFF_MAX_S)
        await asyncio.sleep(wait)


# --- entry point --------------------------------------------------------------


def build_application() -> Application:
    settings = Settings.from_env()
    bot_settings = BotSettings.from_env()
    gmail_settings = GmailSettings.from_env()
    migrate(settings.database_url)
    bot = BillsBot(
        make_session_factory(settings.database_url),
        lambda: Gmail(gmail_settings),
        settings,
        bot_settings,
    )

    async def started(application: Application) -> None:
        bot.start_polling(application.bot)
        logger.info(
            f"Bot up: polling Gmail every {bot_settings.poll_seconds}s, "
            f"{settings.split_percent}% to {settings.roommate_name} "
            f"(@{settings.roommate_venmo})"
        )

    async def stopped(application: Application) -> None:
        await bot.stop_polling()

    application = (
        Application.builder()
        .token(bot_settings.telegram_token)
        .post_init(started)
        .post_shutdown(stopped)
        .build()
    )
    bot.install(application)
    return application


def main() -> None:
    # loguru's default handler prints every variable in a traceback, and the
    # Telegram client's frames hold the bot token.
    logger.remove()
    logger.add(sys.stderr, diagnose=False)
    # bootstrap_retries=-1: started while offline, keep trying to reach
    # Telegram rather than exit.
    build_application().run_polling(
        allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1
    )


if __name__ == "__main__":
    main()
