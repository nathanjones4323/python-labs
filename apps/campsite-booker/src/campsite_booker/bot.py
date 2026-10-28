"""`campsite-booker`: the always-on watcher, with a Telegram bot to talk to.

Long polling, so nothing listens on a port. Every `CAMPSITE_POLL_SECONDS` it
checks every active watch against recreation.gov or ReserveCalifornia
(`service.check_watches`) and sends what's newly open to every allowed user,
each stay with an **Open** link (you add it to your cart on recreation.gov)
and a **Hold** button (the bot tries). A state park's stays have only the
Open link, to ReserveCalifornia. A watch with `auto_hold` gets its first opening
tried straight away, before the alert goes out: a cancellation is usually
gone within minutes. With `auto_pay` too it goes on to pay with the card
saved on the account (a dry run unless CAMPSITE_SUBMIT_PAYMENT).

recreation.gov can answer Add to Cart with a human check (reCAPTCHA). The
bot never tries to answer it: the hold comes back `challenged`, with a link
to add the site yourself and an "It's in my cart" button that pays for it
from the cart (after checking the cart has it).

A watch whose dates aren't on sale yet is armed for the release that opens
them (`releases.py`): you're told when, the sign-in is checked ten minutes
before, and from just before the release the campground is checked every
`CAMPSITE_RELEASE_POLL_SECONDS` until the dates show up.

recreation.gov's own alert emails are a second trigger. Every
`CAMPSITE_GMAIL_POLL_SECONDS` it looks in Gmail for new "Campsite
Available!" emails, checks that campground at once, and records which saw
the opening first, the watcher or the email (`TIMING` lines in the log,
`/timing` here, `campsite-booker-cli timing`).

Holding and paying drive a real browser (`booker`), one at a time.

Commands: /watches, /holds, /check (check now), /login (is the saved
recreation.gov session still signed in?).
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable, Collection
from datetime import date, datetime, timedelta
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

from campsite_booker import booker, messages, service
from campsite_booker.campgrounds import Campgrounds
from campsite_booker.checkout import CartItem, CheckoutResult
from campsite_booker.config import BotSettings, GmailSettings, Settings
from campsite_booker.db import (
    BOOKED,
    BY_EMAIL,
    BY_RELEASE,
    CHALLENGED,
    HELD,
    PAID,
    STOPPED,
    UNKNOWN,
    Hold,
    make_session_factory,
    migrate,
    utcnow,
)
from campsite_booker.gmail import AuthenticationRequired, Gmail
from campsite_booker.recgov import (
    CART_URL,
    RESERVATIONS_URL,
    campground_url,
    campsite_url,
    is_state_park,
)
from campsite_booker.releases import Release, released

SEND_RETRY_S = 120
SEND_BACKOFF_MAX_S = 30
# Hold buttons under one alert (Telegram allows more; a phone screen doesn't).
HOLD_BUTTONS_MAX = 6

HELP = (
    "I watch recreation.gov and ReserveCalifornia (state parks) for campsites "
    "you want and tell you the moment one opens up, with a link to book it "
    "and, on recreation.gov, a button to have me try.\n\n"
    "Set up watches by asking the assistant (or campsite-booker-cli).\n\n"
    "/watches: what I'm watching, with Stop buttons\n"
    "/holds: recent holds\n"
    "/check: check every watch now\n"
    "/timing: the watcher vs recreation.gov's alert emails\n"
    "/login: is the recreation.gov sign-in still good?"
)

HoldFn = Callable[[Settings, Any, int | None], booker.HoldResult]
PayFn = Callable[
    [Settings, str | None, int, str, CartItem | None], tuple[CheckoutResult, Any]
]
# The sign-in is checked this long before a release, so there's time to fix it.
RELEASE_SESSION_CHECK = timedelta(minutes=10)
# Release bursts start this long before the release (clocks differ)...
RELEASE_LEAD = timedelta(seconds=20)
# ...and give up this long after it, back to the normal checks.
RELEASE_BURST = timedelta(minutes=10)


class CampsiteBot:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        recgov: Campgrounds,
        settings: Settings,
        bot_settings: BotSettings,
        hold: HoldFn = booker.hold,
        pay: PayFn = booker.pay,
        signed_in: Callable[[Settings], bool] = booker.check_session,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mailbox: Callable[[], service.Mailbox] | None = None,
    ) -> None:
        self.sessions = sessions
        self.recgov = recgov
        self.settings = settings
        self.bot_settings = bot_settings
        self.hold_fn = hold
        self.pay_fn = pay
        self.signed_in_fn = signed_in
        self.clock = clock
        self.sleep = sleep
        # None: no Gmail sign-in, so no email trigger.
        self.mailbox = mailbox
        self._email_task: asyncio.Task | None = None
        self._email_error: str | None = None
        # /check and the timer mustn't check at once and alert twice.
        self._check_lock = asyncio.Lock()
        # One browser at a time: two holds would fight over the saved session.
        self._browser_lock = asyncio.Lock()
        self._reported_error: str | None = None
        self._poller: asyncio.Task | None = None
        # Release schedules by campground: (fetched at, release).
        self._releases: dict[str, tuple[datetime, Release]] = {}
        # Bursts started, by (campground, release time).
        self._bursts: dict[tuple[str, datetime], asyncio.Task] = {}

    def today(self) -> date:
        return self.clock().astimezone(self.settings.timezone).date()

    def install(self, application: Application) -> None:
        # Group -1 runs before every other handler; raising stops the update.
        application.add_handler(TypeHandler(Update, self._gate), group=-1)
        application.add_handler(CommandHandler(["start", "help"], self._help))
        application.add_handler(CommandHandler("watches", self._watches))
        application.add_handler(CommandHandler("holds", self._holds))
        application.add_handler(CommandHandler("check", self._check))
        application.add_handler(CommandHandler("login", self._login))
        application.add_handler(CommandHandler("timing", self._timing))
        # A Hold tap drives a browser for up to a minute; don't hold up
        # every other update behind it.
        application.add_handler(CallbackQueryHandler(self._on_button, block=False))

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

    async def _watches(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        text, markup = await self._watches_message()
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )

    async def _watches_message(self) -> tuple[str, InlineKeyboardMarkup | None]:
        rows = await asyncio.to_thread(service.list_watches, self.sessions)
        text = messages.watches(rows, self.settings.timezone)
        if not rows:
            return text, None
        return text, InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Stop #{w.id}", callback_data=f"stop:{w.id}:list"
                    )
                ]
                for w in rows
            ]
        )

    async def _holds(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rows = await asyncio.to_thread(service.list_holds, self.sessions)
        await update.effective_message.reply_text(
            messages.holds(rows, self.settings.timezone), parse_mode=ParseMode.HTML
        )

    async def _check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        found = await self.check_once(context.bot)
        if found is None:
            await update.effective_message.reply_text(
                "Checking failed; see the watcher's log."
            )
        elif found == 0:
            await update.effective_message.reply_text("Nothing new is open.")

    async def _timing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary = await asyncio.to_thread(
            service.timing_summary, self.sessions, self.clock() - timedelta(days=30)
        )
        await update.effective_message.reply_text(
            messages.timing(summary, self.settings.timezone, self.mailbox is not None),
            parse_mode=ParseMode.HTML,
        )

    async def _login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with self._browser_lock:
            ok = await asyncio.to_thread(self.signed_in_fn, self.settings)
        if ok:
            await update.effective_message.reply_text(
                "✅ Signed in to recreation.gov; holds will work."
            )
        else:
            await update.effective_message.reply_text(
                messages.login_needed("The saved session is signed out."),
                parse_mode=ParseMode.HTML,
            )

    async def _on_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        # hold:<opening id>, pay:<hold id>, stop:<watch id>[:list],
        # booked:<watch id>
        action, _, rest = (query.data or "").partition(":")
        raw_id, _, origin = rest.partition(":")
        if not raw_id.isdigit():
            await query.answer()
            return
        if action == "hold":
            await query.answer("Holding…")
            await self._hold_from_button(context.bot, int(raw_id))
        elif action == "pay":
            # The tap is the go-ahead; each Pay button pays once at most.
            await query.answer(
                "Paying…" if self.settings.submit_payment else "Dry run…"
            )
            await _edit_markup(query, None)
            await self.pay(context.bot, int(raw_id))
        elif action in ("stop", "booked"):
            reason = BOOKED if action == "booked" else STOPPED
            try:
                watch = await asyncio.to_thread(
                    service.stop_watch, self.sessions, int(raw_id), reason
                )
            except service.NotFound:
                await query.answer("That watch is gone")
                return
            await query.answer(f"Stopped watching #{watch.id}")
            if origin == "list":
                text, markup = await self._watches_message()
                await _edit(query, text, markup)
            elif query.message is not None and getattr(
                query.message, "text_html", None
            ):
                done = (
                    "Booked — stopped watching."
                    if action == "booked"
                    else "Stopped watching."
                )
                await _edit(query, f"{query.message.text_html}\n\n<i>{done}</i>", None)
        else:
            await query.answer()

    async def _hold_from_button(self, bot: Any, opening_id: int) -> None:
        try:
            target = await asyncio.to_thread(
                service.hold_target, self.sessions, opening_id
            )
        except service.NotFound:
            await self._broadcast(bot, "That opening is no longer on record.")
            return
        await self._broadcast(
            bot, messages.holding(target.stay, target.campground_name)
        )
        await self.hold(bot, target, automatic=False)

    # --- holding ----------------------------------------------------------

    async def hold(
        self,
        bot: Any,
        target: service.HoldTarget,
        automatic: bool,
        pay_group_size: int | None = None,
    ) -> None:
        """Hold `target` (and pay, with `pay_group_size`) and tell everyone
        how it went. Not if it's already held (an older Hold button, or a
        second tap), checked under the lock so the first tap is seen."""
        async with self._browser_lock:
            held = await asyncio.to_thread(
                service.live_hold, self.sessions, target.stay, self.clock()
            )
            if held is not None:
                logger.info(
                    f"Not holding {target.stay.campsite_id} on "
                    f"{target.stay.check_in}: hold {held.id} already has it"
                )
                await self._broadcast(
                    bot,
                    messages.already_held(held, self.settings.timezone),
                    reply_markup=_hold_buttons(held, target.watch_id, self.settings),
                )
                return
            try:
                result = await asyncio.to_thread(
                    self.hold_fn, self.settings, target.stay, pay_group_size
                )
            except booker.LoginRequired as e:
                await self._broadcast(bot, messages.login_needed(str(e)))
                return
        record = await asyncio.to_thread(
            service.record_hold,
            self.sessions,
            target,
            result.status,
            result.detail,
            result.page_url,
            self.clock(),
            automatic,
            result.checkout,
        )
        text = messages.hold_result(
            record, self.settings.timezone, result.screenshot is not None
        )
        await self._broadcast(
            bot,
            text,
            reply_markup=_hold_buttons(record, target.watch_id, self.settings),
        )

    async def pay(self, bot: Any, hold_id: int) -> None:
        """Pay for an earlier hold (its Pay button)."""
        # Every allowed user gets a Pay button: read the hold under the
        # browser lock, so a second tap sees what the first one did.
        async with self._browser_lock:
            try:
                record = await asyncio.to_thread(
                    service.get_hold, self.sessions, hold_id
                )
            except service.NotFound:
                await self._broadcast(bot, "That hold is no longer on record.")
                return
            refusal = service.pay_refusal(record)
            if refusal:
                await self._broadcast(bot, refusal)
                return
            group_size = await asyncio.to_thread(
                service.hold_group_size,
                self.sessions,
                record,
                self.settings.group_size,
            )
            try:
                result, screenshot = await asyncio.to_thread(
                    self.pay_fn,
                    self.settings,
                    record.page_url,
                    group_size,
                    f"hold-{hold_id}",
                    service.cart_item(record),
                )
            except booker.LoginRequired as e:
                await self._broadcast(bot, messages.login_needed(str(e)))
                return
        record = await asyncio.to_thread(
            service.record_checkout, self.sessions, hold_id, result, self.clock()
        )
        await self._broadcast(
            bot,
            messages.hold_result(
                record, self.settings.timezone, screenshot is not None
            ),
            reply_markup=_hold_buttons(record, record.watch_id, self.settings),
        )

    # --- polling ----------------------------------------------------------

    def start_polling(self, bot: Any) -> None:
        self._poller = asyncio.create_task(self._poll_forever(bot))
        if self.mailbox is not None and self.bot_settings.gmail_poll_seconds:
            self._email_task = asyncio.create_task(self._email_forever(bot))

    async def stop_polling(self) -> None:
        tasks = [
            t for t in [self._poller, self._email_task, *self._bursts.values()] if t
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_forever(self, bot: Any) -> None:
        while True:
            try:
                await self.check_once(bot)
            except Exception:
                logger.exception("Check failed")
            try:
                await self.arm(bot)
            except Exception:
                logger.exception("Arming for releases failed")
            await self.sleep(self.bot_settings.poll_seconds)

    async def check_once(self, bot: Any) -> int | None:
        """Send anything found earlier but not delivered, then check every
        watch and handle what's new: auto-holds first, then alerts. Returns
        how many new stays turned up, or None if checking failed."""
        async with self._check_lock:
            await self._deliver(
                bot, await asyncio.to_thread(service.unsent_alerts, self.sessions)
            )
            try:
                alerts = await asyncio.to_thread(
                    service.check_watches,
                    self.sessions,
                    self.recgov,
                    self.clock(),
                    self.today(),
                )
            except Exception as exc:
                logger.exception("Checking watches failed")
                await self._problem(bot, f"{type(exc).__name__}: {exc}")
                return None
            self._reported_error = None
            await self._handle(bot, alerts)
            return sum(len(a.stays) for a in alerts)

    async def _handle(self, bot: Any, alerts: list[service.Alert]) -> None:
        """Auto-holds first (the race is the hold), then the alerts."""
        for alert in alerts:
            if alert.auto_hold and await asyncio.to_thread(
                service.claim_auto_hold, self.sessions, alert.watch_id, self.clock()
            ):
                first = alert.stays[0]
                target = service.HoldTarget(
                    first.stay, alert.campground_name, alert.watch_id
                )
                group_size = (
                    (alert.group_size or self.settings.group_size)
                    if alert.auto_pay
                    else None
                )
                await self.hold(bot, target, automatic=True, pay_group_size=group_size)
        await self._deliver(bot, alerts)

    # --- recreation.gov's alert emails ----------------------------------------

    async def _email_forever(self, bot: Any) -> None:
        while True:
            try:
                await self.check_email(bot)
            except Exception:
                logger.exception("Checking alert emails failed")
            await self.sleep(self.bot_settings.gmail_poll_seconds)

    async def check_email(self, bot: Any) -> int:
        """Record new alert emails, check each one's campground straight
        away, and decide the timing of any still undecided. Returns how many
        new emails there were."""
        assert self.mailbox is not None
        mailbox = self.mailbox
        try:
            new = await asyncio.to_thread(
                lambda: service.record_alert_emails(
                    self.sessions,
                    mailbox(),
                    self.recgov,
                    self.settings.timezone,
                    self.clock(),
                )
            )
            self._email_error = None
        except AuthenticationRequired as e:
            if str(e) != self._email_error:
                self._email_error = str(e)
                await self._broadcast(
                    bot, f"⚠️ Can't read recreation.gov's alert emails: {e}"
                )
            return 0
        for email in new:
            if not email.campground_id:
                continue
            async with self._check_lock:
                check = await asyncio.to_thread(
                    service.check_campgrounds,
                    self.sessions,
                    self.recgov,
                    self.clock(),
                    self.today(),
                    {email.campground_id},
                    BY_EMAIL,
                )
                await self._handle(bot, check.alerts)
            finds = sum(len(a.stays) for a in check.alerts)
            if finds:
                await asyncio.to_thread(
                    service.record_triggered_finds, self.sessions, email.gmail_id, finds
                )
        await asyncio.to_thread(service.score_alert_emails, self.sessions, self.clock())
        return len(new)

    # --- releases ---------------------------------------------------------

    async def arm(self, bot: Any) -> None:
        """Find watches waiting on a release, tell you about new ones, and
        start a burst for each release that doesn't have one."""
        waiting = await asyncio.to_thread(
            service.waiting_on_releases,
            self.sessions,
            self.recgov,
            self.clock(),
            self.today(),
            self._releases,
        )
        for item in waiting:
            if not item.announced:
                await self._broadcast(bot, messages.armed(item, self.settings.timezone))
                await asyncio.to_thread(
                    service.mark_announced,
                    self.sessions,
                    [item.watch_id],
                    item.release_at,
                )
            key = (item.campground_id, item.release_at)
            if key not in self._bursts:
                release = self._releases[item.campground_id][1]
                self._bursts[key] = asyncio.create_task(
                    self._burst(bot, item.campground_id, release)
                )
        # Forget finished bursts for releases that have passed.
        for key, task in list(self._bursts.items()):
            if task.done() and key[1] + RELEASE_BURST < self.clock():
                del self._bursts[key]

    async def _wait_until(self, moment: datetime) -> None:
        delay = (moment - self.clock()).total_seconds()
        if delay > 0:
            await self.sleep(delay)

    async def _burst(self, bot: Any, campground_id: str, release: Release) -> None:
        """Check the sign-in ahead of the release, then check the campground
        every few seconds from just before it until its dates appear."""
        assert release.next_at is not None
        try:
            await self._wait_until(release.next_at - RELEASE_SESSION_CHECK)
            async with self._browser_lock:
                ok = await asyncio.to_thread(self.signed_in_fn, self.settings)
            if not ok:
                await self._broadcast(
                    bot,
                    messages.login_needed(
                        "Signed out, with a release at "
                        f"{messages.clock(release.next_at, self.settings.timezone)} "
                        "that a watch is waiting on."
                    ),
                )
            await self._wait_until(release.next_at - RELEASE_LEAD)
            logger.info(f"Release burst for {campground_id} at {release.next_at}")
            while self.clock() < release.next_at + RELEASE_BURST:
                async with self._check_lock:
                    check = await asyncio.to_thread(
                        service.check_campgrounds,
                        self.sessions,
                        self.recgov,
                        self.clock(),
                        self.today(),
                        {campground_id},
                        BY_RELEASE,
                    )
                    await self._handle(bot, check.alerts)
                sites = check.sites.get(campground_id)
                if sites is not None and released(sites, release):
                    logger.info(f"{campground_id}'s release is out")
                    return
                await self.sleep(self.settings.release_poll_seconds)
            logger.warning(f"{campground_id}'s release didn't show up in time")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Release burst for {campground_id} failed")

    async def _deliver(self, bot: Any, alerts: list[service.Alert]) -> None:
        for alert in alerts:
            # An auto-hold just before, or an earlier tap, may have one of
            # these in the cart already: no Hold button for it.
            held = await asyncio.to_thread(
                service.held_openings, self.sessions, alert.stays, self.clock()
            )
            try:
                await self._broadcast(
                    bot,
                    messages.alert(alert, held),
                    reply_markup=_alert_buttons(alert, held),
                )
            except Exception:
                # Left unmarked, so the next check sends it again.
                logger.exception(f"Sending the alert for watch {alert.watch_id} failed")
                continue
            await asyncio.to_thread(
                service.mark_notified,
                self.sessions,
                [s.opening_id for s in alert.stays],
                self.clock(),
            )

    async def _problem(self, bot: Any, error: str) -> None:
        if error == self._reported_error:
            return
        try:
            await self._broadcast(bot, f"⚠️ Couldn't check for campsites: {error}")
        except Exception:
            logger.exception("Reporting the problem failed")
            return
        self._reported_error = error

    async def _broadcast(self, bot: Any, text: str, **kwargs: Any) -> None:
        for chat_id in sorted(self.bot_settings.allowed_user_ids):
            await send_with_retry(bot, chat_id, text, **kwargs)


def _alert_buttons(
    alert: service.Alert, held: Collection[int] = ()
) -> InlineKeyboardMarkup:
    """A row per stay not in the cart yet: Open it on recreation.gov (you add
    it, which gets past a human check), or Hold (the bot tries). A state
    park's stays only open, on ReserveCalifornia."""
    rows = [
        [
            InlineKeyboardButton(messages.open_button_label(s.stay), url=s.stay.url),
        ]
        + (
            [
                InlineKeyboardButton(
                    messages.hold_button_label(s.stay),
                    callback_data=f"hold:{s.opening_id}",
                )
            ]
            if s.stay.holdable
            else []
        )
        for s in [s for s in alert.stays if s.opening_id not in held][:HOLD_BUTTONS_MAX]
    ]
    last = [
        InlineKeyboardButton("Stop watching", callback_data=f"stop:{alert.watch_id}")
    ]
    if len(alert.stays) > HOLD_BUTTONS_MAX or not rows:
        first = alert.stays[0].stay
        site = (
            "ReserveCalifornia"
            if is_state_park(first.campground_id)
            else "recreation.gov"
        )
        last.insert(
            0,
            InlineKeyboardButton(
                f"Open on {site}", url=campground_url(first.campground_id)
            ),
        )
    rows.append(last)
    return InlineKeyboardMarkup(rows)


def _hold_buttons(
    record: Hold, watch_id: int | None, settings: Settings
) -> InlineKeyboardMarkup:
    if record.checkout_status == PAID:
        rows = [[InlineKeyboardButton("Your reservations", url=RESERVATIONS_URL)]]
    elif record.status == HELD and record.checkout_status == UNKNOWN:
        # Confirm was pressed: it may be paid. No second Pay button.
        rows = [
            [InlineKeyboardButton("Your reservations", url=RESERVATIONS_URL)],
            [InlineKeyboardButton("Your cart", url=CART_URL)],
        ]
    elif record.status == HELD:
        label = (
            "💳 Pay with saved card"
            if settings.submit_payment
            else ("💳 Pay (dry run)")
        )
        rows = [
            [InlineKeyboardButton(label, callback_data=f"pay:{record.id}")],
            [InlineKeyboardButton("Pay on recreation.gov", url=CART_URL)],
        ]
    elif record.status == CHALLENGED:
        # You add it on recreation.gov; paying checks the cart has it.
        label = (
            "💳 It's in my cart: pay"
            if settings.submit_payment
            else "💳 It's in my cart: pay (dry run)"
        )
        rows = [
            [
                InlineKeyboardButton(
                    "Open the site", url=campsite_url(record.campsite_id)
                )
            ],
            [InlineKeyboardButton(label, callback_data=f"pay:{record.id}")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    "Open the site", url=campsite_url(record.campsite_id)
                )
            ]
        ]
    if watch_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Booked — stop watching",
                    callback_data=f"booked:{watch_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _edit_markup(query: Any, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except BadRequest as e:
        logger.warning(f"Couldn't update the buttons: {e}")


async def _edit(query: Any, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except BadRequest as e:
        logger.warning(f"Couldn't update the message: {e}")


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
    migrate(settings.database_url)
    gmail_settings = GmailSettings.from_env()
    mailbox = None
    if not bot_settings.gmail_poll_seconds:
        logger.info("Alert emails: off (CAMPSITE_GMAIL_POLL_SECONDS=0)")
    elif not gmail_settings.token_file.exists():
        logger.info(
            "Alert emails: off until you sign in to Gmail (campsite-booker-gmail-login)"
        )
    else:
        mailbox = lambda: Gmail(gmail_settings)  # noqa: E731
    bot = CampsiteBot(
        make_session_factory(settings.database_url),
        Campgrounds(),
        settings,
        bot_settings,
        mailbox=mailbox,
    )

    async def started(application: Application) -> None:
        bot.start_polling(application.bot)
        logger.info(
            f"Watcher up: checking every {bot_settings.poll_seconds}s"
            + (
                f", alert emails every {bot_settings.gmail_poll_seconds}s"
                if mailbox
                else ""
            )
        )

    async def stopped(application: Application) -> None:
        await bot.stop_polling()
        bot.recgov.close()

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
