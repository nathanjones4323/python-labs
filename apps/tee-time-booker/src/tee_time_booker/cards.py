"""What a chat shows: tee-time cards, Confirm cards and run outcomes.

Shared by the Telegram bot (`bot/app.py`, `bot/agent.py`) and the card tools
on the MCP server (`tee_time_booker.assistant`), so a chat through either
looks and books the same. Nothing here talks to Telegram or a model: these
are the cards as rich Markdown, the slots a card may offer, and what a
Confirm on one runs.
"""

import html
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from time import monotonic
from typing import Any

from loguru import logger

# A Confirm or Book button older than this does nothing; ask again.
PAYMENT_TTL_S = 15 * 60
# Most slots on one card.
MAX_OFFERED = 12
# A held booking of one slot from a card, run by a Confirm (not an MCP tool).
BOOK_SLOT = "book_slot"

# Backslash-escaped in values put into a template: Markdown punctuation, the
# HTML brackets and `$`, which opens a formula.
_SPECIAL = re.compile(r"([\\`*_~|\[\]()<>#=+\-!$])")
# A `$` before a digit is a price, never a formula ("$20 or $25" would be one).
_PRICE = re.compile(r"(?<!\\)\$(?=\d)")

RUN_STATUS = {
    "running": "⏳ running",
    "success": "✅ booked",
    "dry_run": "🧪 dry run",
    "unknown": "⚠️ unconfirmed",
    "error": "❌ failed",
    "not_open": "📭 not open yet",
}
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def escape(text: Any) -> str:
    """`text` shown literally inside rich Markdown."""
    return _SPECIAL.sub(r"\\\1", str(text))


def escape_prices(markdown: str) -> str:
    """The model's Markdown with its prices kept out of formulas. Not for
    templates: inside an HTML block a backslash shows as itself."""
    return _PRICE.sub(r"\\$", markdown)


# --- tool results -------------------------------------------------------------

# The calls that can spend money, each with the argument that makes it do so.
# Booking now (`submit_payment`) pays at checkout; scheduling (`auto_book`,
# `enabled`) lets the scheduler pay later without asking.
PAYING_TOOLS: dict[str, str] = {
    "book_plan": "submit_payment",
    "run_request": "submit_payment",
    "create_plan": "auto_book",
    "create_request": "auto_book",
    "set_plan_auto_book": "enabled",
}
# The paying calls that start a run now; the others schedule one for later.
RUN_TOOLS = frozenset({"book_plan", "run_request"})


def asks_to_pay(name: str, arguments: dict[str, Any]) -> bool:
    # Anything but an absent or literal False is held: the API coerces "true"
    # or 1 to True, so an `is True` check would let those pay unconfirmed.
    flag = PAYING_TOOLS.get(name)
    return flag is not None and arguments.get(flag, False) is not False


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False

    def json(self) -> Any:
        """The route's response body. FastMCP wraps a list as {"result": [...]}."""
        data = json.loads(self.text)
        if isinstance(data, dict) and data.keys() == {"result"}:
            return data["result"]
        return data


# Runs one of the MCP server's tools: (name, arguments) -> its result.
CallTool = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


# --- what a chat holds --------------------------------------------------------


@dataclass(frozen=True)
class PendingPayment:
    """A paying call, waiting on the person's Confirm."""

    id: str
    chat_id: int
    tool: str
    arguments: dict[str, Any]
    created: float = field(default_factory=monotonic)

    @property
    def expired(self) -> bool:
        return monotonic() - self.created > PAYMENT_TTL_S


@dataclass(frozen=True)
class StartedRun:
    """A run to watch: the parts of the API's `RunStarted` a chat reports."""

    run_id: str
    course: str
    target_date: str
    dry_run: bool

    @classmethod
    def from_result(cls, result: ToolResult) -> "StartedRun":
        body = result.json()
        return cls(
            run_id=body["run_id"],
            course=body["course"],
            target_date=body["target_date"],
            dry_run=body["dry_run"],
        )


@dataclass(frozen=True)
class TeeTimeOffer:
    """Slots shown on a card, each with a Book button. `slots` are rows of a
    search result, as the API returned them, each with its `course` key;
    `searches` says, per course, who searched for how many (`user_email`,
    `course`, `env_prefix`, `holes`, `players`)."""

    id: str
    chat_id: int
    searches: dict[str, dict[str, Any]]
    slots: tuple[dict[str, Any], ...]
    created: float = field(default_factory=monotonic)

    @property
    def expired(self) -> bool:
        return monotonic() - self.created > PAYMENT_TTL_S


# The latest search per (chat, course, day): the search itself and the slots
# it found. A card may only offer slots from here.
Found = dict[tuple[int, str, str], tuple[dict[str, Any], list[dict[str, Any]]]]


def group_label(search: dict[str, Any]) -> str:
    """'2 players, 18 holes': what a card's Book button books."""
    players = str(search["players"])
    return f"{players} player{'' if players == '1' else 's'}, {search['holes']} holes"


def searched(
    arguments: dict[str, Any], body: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """A search_tee_times call and its result, as (searches, slots found)."""
    found = body["tee_times"]
    searches = {
        slot["course"]: {
            "user_email": arguments.get("user_email"),
            "course": slot["course"],
            "env_prefix": None,
            "holes": arguments.get("holes"),
            "players": arguments.get("players"),
        }
        for slot in found
    }
    return searches, found


def plan_searched(
    plan: dict[str, Any], slots: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """A find_tee_times result: the plan says where and for how many."""
    fields = ("user_email", "course", "env_prefix", "holes", "players")
    searches = {plan["course"]: {key: plan[key] for key in fields}}
    return searches, [{**slot, "course": plan["course"]} for slot in slots]


def remember_found(
    found: Found,
    chat_id: int,
    searches: dict[str, dict[str, Any]],
    slots: list[dict[str, Any]],
) -> None:
    by_place: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for slot in slots:
        by_place.setdefault((slot["course"], slot["start"][:10]), []).append(slot)
    for (course, day), place in by_place.items():
        found[(chat_id, course, day)] = (searches[course], place)


@dataclass
class Picked:
    """The slots a show_tee_times call asked for, sorted out."""

    day: str
    slots: list[dict[str, Any]]
    searches: dict[str, dict[str, Any]]
    # Not in a search result.
    missing: list[str]
    # Slots a tap would book for some other group, from an older search.
    other_group: list[str]


def pick_slots(found: Found, chat_id: int, arguments: dict[str, Any]) -> Picked:
    """The slots of a show_tee_times call that a card may offer: from a
    search in this chat, for the group the person wants."""
    day = str(arguments.get("date"))
    # Compared as text: the model and the API may send either 2 or "2".
    group = (str(arguments.get("players")), str(arguments.get("holes")))
    picked = Picked(day=day, slots=[], searches={}, missing=[], other_group=[])
    for wanted in arguments.get("slots") or []:
        course = str(wanted.get("course"))
        at = str(wanted.get("time"))[:5].zfill(5)
        search, slots = found.get((chat_id, course, day), (None, []))
        slot = next((s for s in slots if s["start"][11:16] == at), None)
        if slot is None:
            picked.missing.append(f"{course} {at}")
        elif (str(search["players"]), str(search["holes"])) != group:
            picked.other_group.append(f"{course} {at} ({group_label(search)})")
        elif slot not in picked.slots:
            picked.slots.append(slot)
            picked.searches[course] = search
    return picked


def make_offer(chat_id: int, picked: Picked) -> TeeTimeOffer:
    return TeeTimeOffer(
        id=secrets.token_urlsafe(8),
        chat_id=chat_id,
        searches=picked.searches,
        slots=tuple(picked.slots[:MAX_OFFERED]),
    )


def nothing_to_show(picked: Picked) -> str:
    """What the model hears when none of its slots can go on a card."""
    content = f"Nothing to show for {picked.day}."
    if picked.missing:
        content += (
            f" Not in a search result: {picked.missing}; search first "
            "(search_tee_times) and offer only slots it found."
        )
    if picked.other_group:
        content += (
            f" Searched for a different group: {picked.other_group}; search again "
            "for the group the person wants."
        )
    return content


def shown(offer: TeeTimeOffer, picked: Picked) -> str:
    """What the model hears once a card is shown."""
    slots = [f"{s['course']} {s['start']}" for s in offer.slots]
    # Said back so the model knows what a tap books: the group is fixed by
    # the search, whatever the person says after the card is shown.
    group = group_label(offer.searches[offer.slots[0]["course"]])
    content = f"Shown as a card for {group} with Book buttons: {slots}."
    if picked.missing:
        content += f" Not in the results, so left out: {picked.missing}."
    if picked.other_group:
        content += (
            f" Searched for a different group, so left out: {picked.other_group}; "
            "search those courses again to offer them."
        )
    return content


def hold_slot(offer: TeeTimeOffer, slot: dict[str, Any]) -> PendingPayment:
    """A Book tap: booking that one slot, waiting on Confirm."""
    return PendingPayment(
        id=secrets.token_urlsafe(8),
        chat_id=offer.chat_id,
        tool=BOOK_SLOT,
        arguments={"search": offer.searches[slot["course"]], "slot": slot},
    )


async def book_slot(
    call: CallTool, arguments: dict[str, Any], user_email: str, pays: bool
) -> ToolResult:
    """A one-off request for exactly this slot, then a run on it."""
    search = arguments["search"]
    start = datetime.fromisoformat(arguments["slot"]["start"])
    request = {
        "user_email": search["user_email"] or user_email,
        "course": search["course"],
        "target_date": start.date().isoformat(),
        "earliest": start.time().isoformat(),
        "latest": start.time().isoformat(),
        "holes": search["holes"],
        "players": search["players"],
    }
    if search.get("env_prefix"):
        request["env_prefix"] = search["env_prefix"]
    created = await call("create_request", request)
    if created.is_error:
        return created
    return await call(
        "run_request",
        {
            "request_id": created.json()["id"],
            "submit_payment": pays,
            "wait_for_release": False,
        },
    )


# --- formatting -------------------------------------------------------------


def format_date(value: str | None) -> str:
    if not value:
        return "?"
    d = date.fromisoformat(value)
    return f"{d:%a %b} {d.day}"


def format_time(value: str) -> str:
    t = time.fromisoformat(value)
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def format_money(value: float) -> str:
    return f"${value:,.2f}".removesuffix(".00")


def format_moment(value: datetime) -> str:
    """A moment in its own (course-local) time, e.g. 'Sat Oct 31, 11:50 PM'."""
    return f"{value:%a %b} {value.day}, {format_time(value.time().isoformat())}"


def _ordinal(n: int) -> str:
    suffix = (
        "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )
    return f"{n}{suffix}"


def schedule_lines(tool: str, booking: dict[str, Any], course: str) -> str:
    """The Confirm card for scheduling a booking: `booking` is a create_plan
    or create_request call's arguments, or a plan the card would arm."""
    details = {"Course": course}
    if tool == "create_plan":
        weekday = WEEKDAYS[int(booking["target_weekday"])]
        details["Day"] = (
            f"{weekday}s, the {_ordinal(int(booking['weeks_ahead']))} ahead"
        )
        if booking.get("next_target_date"):
            details["Next date"] = format_date(booking["next_target_date"])
        title = "**⏰ Book this every week, automatically?**"
        footer = (
            "_Books each next date as soon as it opens and pays at checkout, "
            "without asking again. Pause it any time._"
        )
    else:
        details["Date"] = format_date(str(booking["target_date"]))
        title = "**⏰ Book this automatically?**"
        footer = (
            "_Books it as soon as the day opens and pays at checkout, without "
            "asking again. Cancel it any time before then._"
        )
    details["Tee-off"] = (
        f"{format_time(str(booking['earliest']))} – "
        f"{format_time(str(booking['latest']))}"
    )
    details["Group"] = f"{booking['players']} players · {booking['holes']} holes"
    return "\n\n".join([title, detail_table(details), footer])


def scheduled_status(booking: dict[str, Any]) -> str:
    """What a confirmed schedule card says: when it books, from the plan or
    request the API returned."""
    target = booking.get("next_target_date") or booking.get("target_date")
    when = ""
    if booking.get("first_attempt_at"):
        first = datetime.fromisoformat(booking["first_attempt_at"])
        if first > datetime.now(first.tzinfo):
            when = f" First try {format_moment(first)}."
        else:
            when = " It's bookable already, so it tries at the next chance."
    return f"Scheduled for {format_date(target)}.{when} I'll message you how it goes."


def detail_table(details: dict[str, str]) -> str:
    """Label / value rows. HTML, since a Markdown table needs a header row."""
    cells = "".join(
        f"<tr><td><b>{label}</b></td><td>{html.escape(value)}</td></tr>"
        for label, value in details.items()
    )
    return f"<table compact>{cells}</table>"


def slot_label(slot: dict[str, Any], number: int | None = None) -> str:
    """A tee-time slot on a button: its time (the card's table has the price),
    and its row number when the card spans courses."""
    label = format_time(datetime.fromisoformat(slot["start"]).time().isoformat())
    return f"#{number} {label}" if number is not None else label


def format_offer(offer: TeeTimeOffer, names: dict[str, str]) -> str:
    """The card for `offer`; `names` maps its course keys to course names."""
    day = format_date(offer.slots[0]["start"][:10])
    several = len(names) > 1
    title = day if several else f"{escape(next(iter(names.values())))} · {day}"
    count = len(offer.slots)
    about = [f"{count} {'time' if count == 1 else 'times'}"]
    search = next(iter(offer.searches.values()))
    about += [f"{search['players']} golfers", f"{search['holes']} holes"]
    if several:
        about.append(f"{len(names)} courses")
    # A Side column only when it tells the slots apart.
    sides = len({slot.get("side") for slot in offer.slots}) > 1
    head = '<th align="left">#</th><th align="left">Course</th>' if several else ""
    head += '<th align="left">Time</th>'
    if sides:
        head += '<th align="left">Side</th>'
    head += '<th align="right">Per player</th>'
    rows = []
    for number, slot in enumerate(offer.slots, 1):
        start = datetime.fromisoformat(slot["start"])
        row = ""
        if several:
            row += f"<td>{number}</td><td>{html.escape(names[slot['course']])}</td>"
        row += f"<td><b>{format_time(start.time().isoformat())}</b></td>"
        if sides:
            row += f"<td>{html.escape(slot.get('side') or '')}</td>"
        fee = slot.get("green_fee")
        price = f"<b>{format_money(fee)}</b>" if fee is not None else "–"
        row += f'<td align="right">{price}</td>'
        rows.append(f"<tr>{row}</tr>")
    return "\n\n".join(
        [
            f"📅 **{title}**",
            " · ".join(about),
            f"<table striped compact><tr>{head}</tr>{''.join(rows)}</table>",
            "_Tap a time to book it._",
        ]
    )


def slot_lines(
    slot: dict[str, Any], row: dict[str, Any], course: str, pays: bool
) -> str:
    """The Confirm card for one slot from a tee-time card."""
    start = datetime.fromisoformat(slot["start"])
    side = f" ({slot['side']})" if slot.get("side") else ""
    details = {
        "Course": course,
        "Date": format_date(start.date().isoformat()),
        "Tee-off": f"{format_time(start.time().isoformat())}{side}",
        "Group": f"{row['players']} players · {row['holes']} holes",
    }
    if slot.get("green_fee") is not None:
        per_player = slot["green_fee"] + (slot.get("service_fee") or 0)
        details["Green fee"] = f"{format_money(slot['green_fee'])} / player"
        details["Total"] = f"about {format_money(per_player * row['players'])}"
    lines = [
        "**💳 Book this tee time?**" if pays else "**🧪 Dry run this tee time?**",
        detail_table(details),
        "_Pays at checkout when you confirm._"
        if pays
        else "_Payment is off: the run stops at checkout and pays nothing._",
    ]
    return "\n\n".join(lines)


def payment_lines(payment: PendingPayment, row: dict[str, Any], course: str) -> str:
    """The Confirm card for booking a plan or request now."""
    target = (
        payment.arguments.get("target_date")
        or row.get("target_date")
        or row.get("next_target_date")
    )
    which = "earliest" if row["prefer_earlier"] else "latest"
    details = {
        "Course": course,
        "Date": format_date(target),
        "Tee-off": f"{format_time(row['earliest'])} – {format_time(row['latest'])}",
        "Group": f"{row['players']} players · {row['holes']} holes",
    }
    lines = [
        "**💳 Book and pay?**",
        detail_table(details),
        f"_The run takes the {which} open slot in that window and pays at checkout._",
    ]
    if payment.arguments.get("wait_for_release", True) and not payment.arguments.get(
        "target_date"
    ):
        lines.append(
            "_If that day isn't released yet, it waits for the release first._"
        )
    return "\n\n".join(lines)


async def course_name(call: CallTool, key: str) -> str:
    looked_up = await call("get_course", {"key": key})
    return key if looked_up.is_error else looked_up.json()["name"]


async def describe_payment(call: CallTool, payment: PendingPayment, pays: bool) -> str:
    """What a Confirm will book, read from the plan or request itself rather
    than from anything the model said. Only a booking that doesn't exist yet
    (a slot from a card, a new schedule) is described from the call, and the
    call is exactly what a Confirm runs."""
    fallback = (
        f"**💳 Book and pay?**\n\n{escape(f'{payment.tool} {payment.arguments}')}"
    )
    try:
        if payment.tool == BOOK_SLOT:
            search = payment.arguments["search"]
            course = await course_name(call, str(search["course"]))
            return slot_lines(payment.arguments["slot"], search, course, pays)
        if payment.tool in ("create_plan", "create_request"):
            course = await course_name(call, str(payment.arguments["course"]))
            return schedule_lines(payment.tool, payment.arguments, course)
        if payment.tool in ("book_plan", "set_plan_auto_book"):
            looked_up = await call(
                "get_plan", {"plan_id": payment.arguments.get("plan_id")}
            )
        else:
            looked_up = await call(
                "get_request", {"request_id": payment.arguments.get("request_id")}
            )
        if looked_up.is_error:
            return fallback
        row = looked_up.json()
        course = await course_name(call, row["course"])
        if payment.tool == "set_plan_auto_book":
            return schedule_lines("create_plan", row, course)
        return payment_lines(payment, row, course)
    except (KeyError, TypeError, ValueError):
        # Malformed model arguments (e.g. an unparseable target_date) must
        # not cost the person their Confirm / Cancel buttons.
        logger.exception("Describing a held payment")
        return fallback


def _slot(run: dict[str, Any]) -> str:
    if not run.get("tee_time_local"):
        return ""
    start = datetime.fromisoformat(run["tee_time_local"])
    side = f" ({run['side']})" if run.get("side") else ""
    return f"{format_time(start.time().isoformat())}{side}"


def format_outcome(
    started: StartedRun, run: dict[str, Any], course: str | None = None
) -> str:
    """`course` is the course's name; without it the key stands in."""
    name = escape(course or started.course)
    where = f"{name} on {format_date(started.target_date)}"
    slot = escape(_slot(run))
    fee = (
        f"\n\nGreen fee {escape(format_money(float(run['green_fee'])))} per player."
        if run.get("green_fee")
        else ""
    )
    match run["status"]:
        case "success":
            # In code, so it's one tap to copy.
            confirmation = (
                f"\n\nConfirmation: `{run['confirmation'].replace('`', '')}`"
                if run.get("confirmation")
                else ""
            )
            at = f" at **{slot}**" if slot else ""
            return f"✅ **Booked** {where}{at}.{fee}{confirmation}"
        case "dry_run":
            return (
                f"🧪 **Dry run done:** reached checkout for {slot or 'a slot'} at "
                f"{where}. Nothing was paid.{fee}"
            )
        case "unknown":
            return (
                f"⚠️ **Payment submitted, no confirmation** for {where} {slot}.\n\n"
                "Check your account on the booking site before trying again."
            )
        case "not_open":
            return (
                f"📭 **Not open yet:** {where} isn't in the site's booking window "
                "yet. Nothing was booked."
            )
        case _:
            detail = html.escape(run.get("detail") or "no detail")
            return (
                f"❌ **The run failed** for {where}.\n\n"
                f"<blockquote expandable>{detail}</blockquote>"
            )


def format_runs(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "No runs yet."
    rows = ["| Started | Status | Slot |", "|:--|:--|:--|"]
    for run in runs:
        started = datetime.fromisoformat(run["started_at"]).astimezone()
        status = RUN_STATUS.get(run["status"], escape(run["status"]))
        rows.append(
            f"| {started:%a %b} {started.day} {format_time(started.time().isoformat())}"
            f" | {status} | {escape(_slot(run)) or '–'} |"
        )
    return "\n".join(rows)
