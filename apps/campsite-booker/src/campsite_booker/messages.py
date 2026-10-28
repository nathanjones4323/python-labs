"""The Telegram text (HTML), kept apart from the bot so it's easy to test."""

from collections.abc import Collection
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from campsite_booker.db import (
    BY_EMAIL,
    CHALLENGED,
    DRY_RUN,
    FAILED,
    HELD,
    PAID,
    Hold,
    Watch,
    utcnow,
)
from campsite_booker.recgov import Stay
from campsite_booker.service import Alert, TimingSummary, Waiting

# Stays listed in one alert; the rest are counted.
ALERT_LIST_MAX = 8
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
    nights = f"{stay.nights} night{'s' if stay.nights != 1 else ''}"
    loop = f", {escape(stay.loop)}" if stay.loop else ""
    return (
        f"Site {escape(stay.site)}{loop} ({escape(site_kind(stay.campsite_type))}, "
        f"up to {stay.max_people}): {day(stay.check_in)} → {day(stay.check_out)}, "
        f"{nights}"
    )


def alert(item: Alert, held: Collection[int] = ()) -> str:
    """The alert for `item`; stays whose opening id is in `held` are already
    in the cart and say so."""
    count = len(item.stays)
    lines = [
        f"🏕 <b>Open at {escape(item.campground_name)}</b>",
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
    if any(s.opening_id not in held for s in item.stays):
        lines += [
            "",
            "Open one to add it to your cart on recreation.gov, or tap 🤖 Hold "
            "and I'll try (recreation.gov may want a human check first).",
        ]
    return "\n".join(lines)


def open_button_label(stay: Stay) -> str:
    return f"Open {stay.site} · {short_day(stay.check_in)}"


def hold_button_label(stay: Stay) -> str:
    return f"🤖 Hold {stay.site}"


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
