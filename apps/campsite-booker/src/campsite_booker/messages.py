"""The Telegram text (HTML) and buttons, kept apart from the bot so it's easy
to test, and so the MCP server's cards (for the assistant) match the bot's.

Buttons are rows of `{"text", "data"}` (a tap comes back as `data`) or
`{"text", "url"}` (opens a link); the bot turns them into a keyboard. A
`keep` button's tap leaves the message and its other buttons in place: the
assistant's Telegram plugin reads it, and the bot never edits an alert on a
Hold tap anyway."""

from collections.abc import Collection
from datetime import date, datetime
from html import escape
from typing import TypedDict
from zoneinfo import ZoneInfo

from campsite_booker.db import (
    BY_EMAIL,
    CHALLENGED,
    DRY_RUN,
    FAILED,
    HELD,
    PAID,
    UNKNOWN,
    Hold,
    Watch,
    utcnow,
)
from campsite_booker.recgov import (
    CART_URL,
    RESERVATIONS_URL,
    Stay,
    campground_url,
    campsite_url,
    is_state_park,
)
from campsite_booker.service import Alert, TimingSummary, Waiting

# Stays listed in one alert; the rest are counted.
ALERT_LIST_MAX = 8
# Stay buttons under one alert (Telegram allows more; a phone screen doesn't).
HOLD_BUTTONS_MAX = 6


class Button(TypedDict, total=False):
    text: str
    data: str
    url: str
    keep: bool


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%b')} {d.day}"


def short_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def clock(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%-I:%M %p")


def site_kind(campsite_type: str) -> str:
    """ "TENT ONLY NONELECTRIC" → "tent only, nonelectric"."""
    words = campsite_type.lower().split()
    if words and words[-1] in ("electric", "nonelectric"):
        return f"{' '.join(words[:-1])}, {words[-1]}"
    return " ".join(words)


def stay_line(stay: Stay) -> str:
    """The site links to its page: the buttons are for holding."""
    nights = f"{stay.nights} night{'s' if stay.nights != 1 else ''}"
    loop = f", {escape(stay.loop)}" if stay.loop else ""
    # ReserveCalifornia doesn't say how many a site holds.
    people = f", up to {stay.max_people}" if stay.max_people else ""
    return (
        f'<a href="{escape(stay.url)}">Site {escape(stay.site)}</a>{loop}'
        f" ({escape(site_kind(stay.campsite_type))}"
        f"{people}): {day(stay.check_in)} → {day(stay.check_out)}, {nights}"
    )


def alert(item: Alert, held: Collection[int] = ()) -> str:
    """The alert for `item`; stays whose opening id is in `held` are already
    in the cart and say so."""
    count = len(item.stays)
    area = f" · {escape(item.area)}" if item.area else ""
    lines = [
        f"🏕 <b>Open at {escape(item.campground_name)}</b>{area}",
        f"<i>{escape(item.watch_name)}</i>",
        "",
    ]
    lines += [
        f"• {stay_line(s.stay)}" + (" (in your cart)" if s.opening_id in held else "")
        for s in item.stays[:ALERT_LIST_MAX]
    ]
    if count > ALERT_LIST_MAX:
        lines.append(f"…and {count - ALERT_LIST_MAX} more")
    if item.found_by == BY_EMAIL:
        lines += ["", "📧 Found checking right after recreation.gov's alert email."]
    if item.stays and is_state_park(item.stays[0].stay.campground_id):
        lines += ["", "Open one to book it on ReserveCalifornia."]
    elif any(s.opening_id not in held for s in item.stays):
        lines += [
            "",
            "Tap a site above to add it to your cart on recreation.gov, or tap "
            "🤖 Hold and I'll try (recreation.gov may want a human check first).",
        ]
    return "\n".join(lines)


def short_range(stay: Stay) -> str:
    """ "Nov 6–8", or "Oct 30–Nov 1" across a month."""
    start, end = stay.check_in, stay.check_out
    if start.month == end.month:
        return f"{short_day(start)}–{end.day}"
    return f"{short_day(start)}–{short_day(end)}"


def stay_name(stay: Stay, area: str) -> str:
    """ "Big Sur · Site 040 · Nov 6–8": a button says where, which site and
    when, so it reads without the list above."""
    where = f"{area} · " if area else ""
    return f"{where}Site {stay.site} · {short_range(stay)}"


def open_button_label(stay: Stay, area: str) -> str:
    return f"Open {stay_name(stay, area)}"


def hold_button_label(stay: Stay, area: str) -> str:
    return f"🤖 Hold {stay_name(stay, area)}"


def alert_buttons(
    alert: Alert, held: Collection[int] = (), prefix: str = ""
) -> list[list[Button]]:
    """A full-width row per stay not in the cart yet: Hold (the bot tries),
    or, for a state park, Open it on ReserveCalifornia. To add a
    recreation.gov stay yourself (past a human check), tap its site in the
    list. `prefix` goes before each button's data."""
    rows: list[list[Button]] = [
        [
            Button(
                text=hold_button_label(s.stay, alert.area),
                data=f"{prefix}hold:{s.opening_id}",
                keep=True,
            )
            if s.stay.holdable
            else Button(text=open_button_label(s.stay, alert.area), url=s.stay.url)
        ]
        for s in [s for s in alert.stays if s.opening_id not in held][:HOLD_BUTTONS_MAX]
    ]
    last: list[Button] = []
    if alert.watch_id is not None:
        last.append(Button(text="Stop watching", data=f"{prefix}stop:{alert.watch_id}"))
    if len(alert.stays) > HOLD_BUTTONS_MAX or not rows:
        first = alert.stays[0].stay
        site = (
            "ReserveCalifornia"
            if is_state_park(first.campground_id)
            else "recreation.gov"
        )
        last.insert(
            0, Button(text=f"Open on {site}", url=campground_url(first.campground_id))
        )
    if last:
        rows.append(last)
    return rows


def hold_buttons(
    record: Hold, watch_id: int | None, submit_payment: bool, prefix: str = ""
) -> list[list[Button]]:
    """What can be done with a hold: pay for it, look at it, or stop the
    watch that found it."""
    if record.checkout_status == PAID:
        rows = [[Button(text="Your reservations", url=RESERVATIONS_URL)]]
    elif record.status == HELD and record.checkout_status == UNKNOWN:
        # Confirm was pressed: it may be paid. No second Pay button.
        rows = [
            [Button(text="Your reservations", url=RESERVATIONS_URL)],
            [Button(text="Your cart", url=CART_URL)],
        ]
    elif record.status == HELD:
        label = "💳 Pay with saved card" if submit_payment else "💳 Pay (dry run)"
        rows = [
            [Button(text=label, data=f"{prefix}pay:{record.id}")],
            [Button(text="Pay on recreation.gov", url=CART_URL)],
        ]
    elif record.status == CHALLENGED:
        # You add it on recreation.gov; paying checks the cart has it.
        label = (
            "💳 It's in my cart: pay"
            if submit_payment
            else "💳 It's in my cart: pay (dry run)"
        )
        rows = [
            [Button(text="Open the site", url=campsite_url(record.campsite_id))],
            [Button(text=label, data=f"{prefix}pay:{record.id}")],
        ]
    else:
        rows = [[Button(text="Open the site", url=campsite_url(record.campsite_id))]]
    if watch_id is not None:
        rows.append(
            [
                Button(
                    text="✅ Booked — stop watching",
                    data=f"{prefix}booked:{watch_id}",
                )
            ]
        )
    return rows


def ask_auto_pay(watch: Watch, submit_payment: bool) -> str:
    """Asks to let `watch` pay for its automatic hold (the assistant's card)."""
    mode = (
        "It pays with the card saved on your recreation.gov account."
        if submit_payment
        else "Payment is off, so it stops at checkout and pays nothing."
    )
    return (
        f"💳 <b>Pay automatically?</b>\n\n"
        f"When <b>{escape(watch.campground_name)}</b> "
        f"({escape(watch.name)}) opens up, hold the first site and "
        f"pay for it without asking. {mode}"
    )


def ask_auto_pay_buttons(watch_id: int, prefix: str = "") -> list[list[Button]]:
    return [
        [
            Button(text="Turn on auto-pay", data=f"{prefix}autopay:{watch_id}"),
            Button(text="Not now", data=f"{prefix}none"),
        ]
    ]


def holding(stay: Stay, campground_name: str) -> str:
    return (
        f"⏳ Putting site {escape(stay.site)} at {escape(campground_name)} "
        f"({short_day(stay.check_in)} → {short_day(stay.check_out)}) in your cart…"
    )


def hold_result(hold: Hold, tz: ZoneInfo, screenshot_note: bool = False) -> str:
    where = (
        f"site {escape(hold.site)} at {escape(hold.campground_name)}, "
        f"{day(hold.check_in)} → {day(hold.check_out)}"
    )
    auto = "Auto-held: " if hold.automatic else ""
    if hold.checkout_status == PAID:
        number = f" Order {escape(hold.confirmation)}." if hold.confirmation else ""
        return (
            f"🎉 <b>Booked and paid</b>: {where}.{number}\n"
            "recreation.gov emails the confirmation."
        )
    if hold.status == HELD:
        until = clock(hold.expires_at, tz) if hold.expires_at else "15 minutes"
        text = f"✅ <b>{auto}In your cart</b>: {where}.\n\n"
        if hold.checkout_status == DRY_RUN:
            text += f"🧪 {escape(hold.checkout_detail or '')}\n\n"
        elif hold.checkout_status:
            text += f"⚠️ Paying didn't finish: {escape(hold.checkout_detail or '')}\n\n"
        text += (
            f"It's yours until about <b>{until}</b>. Pay before then, or it's released."
        )
        if screenshot_note and hold.checkout_status:
            text += "\n<i>A screenshot and the page are in the artifacts folder.</i>"
        return text
    detail = escape(hold.detail or "")
    if hold.status == CHALLENGED:
        text = (
            f"🧩 {'Tried to auto-hold: ' if hold.automatic else ''}"
            f"<b>recreation.gov wants a human check</b> before {where} "
            "goes in the cart, so it isn't held yet.\n\n"
            "Tap <b>Open the site</b>, pick the dates and Add to Cart yourself "
            "(answer the check if it asks). Then tap <b>It's in my cart</b> "
            "and I'll do the checkout."
        )
        if hold.checkout_status:
            text += f"\n\n⚠️ {escape(hold.checkout_detail or '')}"
        return text
    if hold.status == FAILED:
        text = f"❌ {auto}Couldn't hold {where}.\n{detail}"
    else:
        text = f"❓ {auto}Not sure the hold worked for {where}.\n{detail}"
    if screenshot_note:
        text += "\n<i>A screenshot and the page are saved in the artifacts folder.</i>"
    return text


def already_held(hold: Hold, tz: ZoneInfo) -> str:
    """A Hold tap for a stay that's already in the cart, or paid."""
    return "Already done, not holding it again.\n\n" + hold_result(hold, tz)


def when(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{day(local.date())} {clock(moment, tz)}"


def armed(item: Waiting, tz: ZoneInfo) -> str:
    return (
        f"⏰ <b>{escape(item.campground_name)}</b>: the dates for "
        f"<i>{escape(item.watch_name)}</i> go on sale "
        f"<b>{when(item.release_at, tz)}</b>.\n\n"
        "I'll check the sign-in 10 minutes before, then watch every couple of "
        "seconds from just before it opens."
    )


def login_needed(reason: str) -> str:
    return (
        f"⚠️ <b>Sign-in to recreation.gov needed</b>\n{escape(reason)}\n\n"
        "On the computer running the watcher:\n"
        "<code>cd apps/campsite-booker && uv run --package campsite-booker "
        "campsite-booker-login</code>"
    )


def watch_line(watch: Watch, tz: ZoneInfo) -> str:
    if watch.arrival_from == watch.arrival_to:
        dates = day(watch.arrival_from)
    else:
        dates = f"arriving {day(watch.arrival_from)}–{day(watch.arrival_to)}"
    if watch.arrival_weekdays:
        dates += (
            " (" + "/".join(WEEKDAYS[d] for d in sorted(watch.arrival_weekdays)) + ")"
        )
    bits = [f"{watch.nights} night{'s' if watch.nights != 1 else ''}"]
    if watch.group_size:
        bits.append(f"{watch.group_size} people")
    if watch.campsite_ids:
        bits.append(f"{len(watch.campsite_ids)} chosen site(s)")
    if watch.site_types:
        bits.append("/".join(t.lower() for t in watch.site_types))
    if watch.auto_hold:
        kind = "auto-hold & pay" if watch.auto_pay else "auto-hold"
        bits.append(f"{kind} used" if watch.auto_held_at else kind)
    if watch.armed_release_at and watch.armed_release_at > utcnow():
        bits.append(f"⏰ on sale {when(watch.armed_release_at, tz)}")
    status = ""
    if not watch.active:
        status = f" — ended ({watch.ended_reason})"
    elif watch.last_error:
        status = f" — ⚠️ {escape(watch.last_error)}"
    elif watch.last_checked_at:
        status = f" — checked {clock(watch.last_checked_at, tz)}"
    return (
        f"<b>#{watch.id} {escape(watch.campground_name)}</b>: {dates}, "
        f"{', '.join(bits)}{status}"
    )


def watches(rows: list[Watch], tz: ZoneInfo) -> str:
    if not rows:
        return (
            "No watches. Ask the assistant to watch a campground for you, or "
            "use <code>campsite-booker-cli watch</code>."
        )
    return "👀 <b>Watching</b>\n\n" + "\n".join(watch_line(w, tz) for w in rows)


def holds(rows: list[Hold], tz: ZoneInfo) -> str:
    if not rows:
        return "No holds yet."
    lines = ["🧾 <b>Recent holds</b>", ""]
    for hold in rows:
        mark = {"held": "✅", "failed": "❌"}.get(hold.status, "❓")
        lines.append(
            f"{mark} {hold.created_at.astimezone(tz):%b %-d} "
            f"{clock(hold.created_at, tz)}: "
            f"site {escape(hold.site)} at {escape(hold.campground_name)}, "
            f"{short_day(hold.check_in)} → {short_day(hold.check_out)}"
            + (
                f" — {escape(hold.detail)}"
                if hold.status != HELD and hold.detail
                else ""
            )
        )
    return "\n".join(lines)


OUTCOME_LABELS = {
    "watcher_first": "watcher first",
    "email_first": "email first",
    "missed": "watcher never saw it",
    "not_watched": "no watch there",
    "pending": "still deciding",
}


def _secs(value: float | None) -> str:
    if value is None:
        return "–"
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value / 60:.1f} min" if value >= 120 else f"{sign}{value:.0f} s"


def timing(summary: TimingSummary, tz: ZoneInfo, email_on: bool) -> str:
    """The watcher against recreation.gov's alert emails, last 30 days."""
    if not summary.rows:
        if not email_on:
            return (
                "Alert emails are off: sign in to Gmail with "
                "<code>campsite-booker-gmail-login</code> to compare."
            )
        return "No recreation.gov alert emails in the last 30 days yet."
    lines = ["⏱ <b>Watcher vs alert emails</b> (30 days)", ""]
    lines += [
        f"{OUTCOME_LABELS.get(k, k)}: {n}" for k, n in sorted(summary.counts.items())
    ]
    lines += [
        "",
        f"Watcher ahead by (median): {_secs(summary.median_lead_s)}",
        f"recreation.gov to inbox (median): {_secs(summary.median_email_delay_s)}",
        "",
    ]
    for row in summary.rows[:8]:
        lines.append(
            f"{row.received_at.astimezone(tz):%b %-d} {clock(row.received_at, tz)} "
            f"{escape(row.campground_name or '?')} "
            f"{escape(','.join(row.sites) or '?')}: "
            f"{OUTCOME_LABELS.get(row.outcome or 'pending', row.outcome)}"
            + (f" ({_secs(row.lead_seconds)})" if row.lead_seconds is not None else "")
        )
    return "\n".join(lines)
