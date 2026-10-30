"""The app as MCP tools, for the assistant (or Claude Desktop / Claude Code).

    campsite-booker-mcp          # stdio

Searching and reading watches touch only recreation.gov's and
ReserveCalifornia's public APIs and the local database. `hold_campsite`
drives a signed-in browser and puts a site in the account's cart; paying
for it is the client's to gate (the assistant: a tap). Watches are checked
by the watcher (`campsite-booker`), not here; a watch made here is picked
up on its next check as long as it runs against the same database.

For an agent that is its own Telegram bot (the assistant), results that the
watcher's bot would show come with `telegram`: the bot's own card (HTML) and
buttons (`campsite_booker.messages`), to send as given. Button data is
`camp:<action>:<id>` and a tap goes to `press_button`, which does what the
bot does for that button: `hold` holds, `pay` pays for a hold, `autopay`
turns on a watch's auto-pay, `stop` / `booked` stop a watch. Only `pay` and
`autopay` spend money; the assistant lets those through only on the turn
the person's tap of that very button started.
"""

import asyncio
import itertools
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from pydantic import Field

from campsite_booker import booker, messages, service
from campsite_booker.availability import StayCriteria, parse_weekdays
from campsite_booker.campgrounds import Campgrounds
from campsite_booker.config import Settings
from campsite_booker.db import (
    BOOKED,
    HELD,
    PAID,
    STOPPED,
    Hold,
    Watch,
    make_session_factory,
    migrate,
    utcnow,
)
from campsite_booker.messages import WEEKDAYS
from campsite_booker.recgov import (
    CART_URL,
    Campground,
    RecGovError,
    Stay,
    is_state_park,
)
from campsite_booker.releases import parse_releases, waiting_on

INSTRUCTIONS = """
Campsites on recreation.gov (federal campgrounds: national parks and forests,
Army Corps lakes, BLM) and ReserveCalifornia (California state parks and
beaches, e.g. Pfeiffer Big Sur). Other states' parks aren't covered.

- `search_campgrounds` finds campground ids by name or near a point, on
  both sites. State park ids start with `rc-` (`source: reservecalifornia`).
- State parks can be searched and watched, and alerts link to
  ReserveCalifornia to book, but nothing here holds or pays for them:
  `auto_hold`, `hold_campsite` and `set_watch_auto_pay` are recreation.gov
  only. ReserveCalifornia opens each day six months ahead at 8 AM Pacific;
  there's no release to arm for.
- `find_open_campsites` says what's bookable now for some dates.
- `create_watch` alerts on Telegram when a site opens up (cancellations),
  and with `auto_hold` puts the first one straight in the cart.
- `hold_campsite` puts one site in the cart now (15 minutes to pay). With
  `pay: true` it carries on and pays with the card saved on the account;
  `pay_hold` pays for an earlier hold. Both are dry runs (stop at Review and
  Confirm, nothing paid) unless the operator turned payment on; the result
  says which.
- recreation.gov may answer Add to Cart with a human check (reCAPTCHA),
  which nothing here answers: the hold comes back `challenged`. Give the
  person the site's link to add it to their cart themselves; once they say
  it's there, `pay_hold` can pay for it (it checks the cart has it).
- `set_watch_auto_pay` makes a watch pay for its automatic hold too.
- A `telegram` field in a result is the watcher bot's own card for it
  (`text`, `format`, `buttons`): send it as given. A tapped `camp:` button
  goes to `press_button`. Paying is a tap on a card's Pay button
  (`ask_auto_pay` draws the one for a watch's auto-pay).
- A watch for dates not on sale yet is armed for the release that opens
  them: `create_watch` says when (`on_sale_at`), and the watcher polls every
  couple of seconds from just before it.

Dates are YYYY-MM-DD, campground-local. A stay is `nights` nights from the
arrival date; flexible trips give an arrival range and optionally weekdays.
""".strip()

# Stays returned per campground by a search; the rest are counted.
STAYS_PER_CAMPGROUND = 15
# Button data prefix: the assistant's chat also has other apps' buttons.
PREFIX = "camp:"
# Search-card stays kept for their Hold buttons (up to 8 per campground).
FOUND_MAX = 200


def card(text: str, rows: list[list[messages.Button]] | None = None) -> dict:
    """A message for the assistant to send as is: HTML, as the bot sends."""
    out: dict[str, Any] = {"text": text, "format": "html"}
    if rows:
        out["buttons"] = rows
    return out


Dates = Annotated[date, Field(description="YYYY-MM-DD")]
Weekdays = Annotated[
    list[str] | None,
    Field(description='Only arrive on these days, e.g. ["Fri", "Sat"]'),
]
SiteTypes = Annotated[
    list[str] | None,
    Field(
        description='Site type must contain one of these words, e.g. ["TENT"] '
        'or ["RV"]; case-insensitive'
    ),
]


def criteria(
    arrival_from: date,
    arrival_to: date | None,
    nights: int,
    arrival_weekdays: list[str] | None = None,
    group_size: int | None = None,
    site_types: list[str] | None = None,
    campsite_ids: list[str] | None = None,
    include_group_sites: bool = False,
) -> StayCriteria:
    try:
        return StayCriteria(
            arrival_from=arrival_from,
            arrival_to=arrival_to or arrival_from,
            nights=nights,
            arrival_weekdays=parse_weekdays(arrival_weekdays),
            group_size=group_size,
            site_types=tuple(site_types or ()),
            campsite_ids=frozenset(str(c) for c in campsite_ids or ()),
            include_group_sites=include_group_sites,
        )
    except ValueError as e:
        raise ToolError(f"Invalid: {e}") from e


def campground_dict(c: Campground) -> dict[str, Any]:
    return {
        "campground_id": c.id,
        "name": c.name,
        "park": c.park,
        "city": c.city,
        "state": c.state,
        "distance_miles": c.distance_miles,
        "price_per_night": (
            None if c.price_min is None else {"min": c.price_min, "max": c.price_max}
        ),
        "url": c.url,
        "source": "reservecalifornia" if is_state_park(c.id) else "recreation.gov",
    }


def stay_dict(s: Stay) -> dict[str, Any]:
    return {
        "campsite_id": s.campsite_id,
        "site": s.site,
        "loop": s.loop,
        "type": s.campsite_type,
        "max_people": s.max_people or None,
        "check_in": s.check_in.isoformat(),
        "check_out": s.check_out.isoformat(),
        "nights": s.nights,
        "url": s.url,
    }


def watch_dict(w: Watch) -> dict[str, Any]:
    return {
        "watch_id": w.id,
        "name": w.name,
        "campground_id": w.campground_id,
        "campground_name": w.campground_name,
        "campsite_ids": w.campsite_ids,
        "arrival_from": w.arrival_from.isoformat(),
        "arrival_to": w.arrival_to.isoformat(),
        "nights": w.nights,
        "arrival_weekdays": [WEEKDAYS[d] for d in w.arrival_weekdays],
        "group_size": w.group_size,
        "site_types": w.site_types,
        "auto_hold": w.auto_hold,
        "auto_pay": w.auto_pay,
        "auto_hold_used": w.auto_held_at is not None,
        "waiting_on_release_at": (
            w.armed_release_at.isoformat() if w.armed_release_at else None
        ),
        "active": w.active,
        "ended_reason": w.ended_reason,
        "last_checked_at": w.last_checked_at.isoformat() if w.last_checked_at else None,
        "last_error": w.last_error,
    }


def hold_dict(h: Hold) -> dict[str, Any]:
    return {
        "hold_id": h.id,
        "status": h.status,
        "detail": h.detail,
        "campground_name": h.campground_name,
        "site": h.site,
        "campsite_id": h.campsite_id,
        "check_in": h.check_in.isoformat(),
        "check_out": h.check_out.isoformat(),
        "expires_at": h.expires_at.isoformat() if h.expires_at else None,
        "checkout_status": h.checkout_status,
        "checkout_detail": h.checkout_detail,
        "confirmation": h.confirmation,
        "pay_at": CART_URL if h.status == HELD and h.checkout_status != PAID else None,
        "automatic": h.automatic,
        "created_at": h.created_at.isoformat(),
    }


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    migrate(settings.database_url)
    sessions = make_session_factory(settings.database_url)
    recgov = Campgrounds()
    mcp = FastMCP(name="campsite-booker", instructions=INSTRUCTIONS)

    def today() -> date:
        return datetime.now(settings.timezone).date()

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except (RecGovError, service.NotFound, ValueError) as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e

    # Stays a search card offered, by the number on their Hold button; only
    # the latest FOUND_MAX, as an old card's openings have likely gone.
    found: dict[int, service.HoldTarget] = {}
    numbers = itertools.count(1)

    def offer(target: service.HoldTarget) -> int:
        key = next(numbers)
        found[key] = target
        while len(found) > FOUND_MAX:
            del found[next(iter(found))]
        return key

    def hold_card(hold: Hold) -> dict[str, Any]:
        text = messages.hold_result(hold, settings.timezone)
        rows = messages.hold_buttons(
            hold, hold.watch_id, settings.submit_payment, prefix=PREFIX
        )
        return {**hold_dict(hold), "telegram": card(text, rows)}

    async def hold_target(
        target: service.HoldTarget, pay_group_size: int | None
    ) -> dict[str, Any]:
        """Hold (and with `pay_group_size`, pay for) `target`, unless it's
        already in the cart or paid for."""
        stay = target.stay
        held = await run(service.live_hold, sessions, stay, utcnow())
        if held is not None:
            logger.info(
                f"Not holding {stay.campsite_id} on {stay.check_in}: "
                f"hold {held.id} already has it"
            )
            out = hold_card(held)
            out["telegram"]["text"] = messages.already_held(held, settings.timezone)
            return out
        try:
            result = await asyncio.to_thread(
                booker.hold, settings, stay, pay_group_size
            )
        except booker.LoginRequired as e:
            raise ToolError(f"LoginRequired: {e}") from e
        hold = await run(
            service.record_hold,
            sessions,
            target,
            result.status,
            result.detail,
            result.page_url,
            utcnow(),
            checkout=result.checkout,
        )
        return hold_card(hold)

    async def pay(hold_id: int) -> dict[str, Any]:
        hold = await run(service.get_hold, sessions, hold_id)
        if hold.checkout_status == PAID:
            return hold_card(hold)
        refusal = service.pay_refusal(hold)
        if refusal:
            raise ToolError(refusal)
        group_size = await run(
            service.hold_group_size, sessions, hold, settings.group_size
        )
        try:
            result, _ = await asyncio.to_thread(
                booker.pay,
                settings,
                hold.page_url,
                group_size,
                f"hold-{hold_id}",
                service.cart_item(hold),
            )
        except booker.LoginRequired as e:
            raise ToolError(f"LoginRequired: {e}") from e
        return hold_card(
            await run(service.record_checkout, sessions, hold_id, result, utcnow())
        )

    @mcp.tool
    async def search_campgrounds(
        query: Annotated[
            str | None, Field(description="Name, park or place, e.g. 'Upper Pines'")
        ] = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_miles: Annotated[float, Field(gt=0, le=300)] = 50,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        """Find campgrounds on recreation.gov and ReserveCalifornia (state
        parks) by text, or near a point (nearest first). Returns ids for the
        other tools."""
        if latitude is not None and longitude is not None:
            found = await run(
                recgov.search_near, latitude, longitude, radius_miles, limit
            )
        elif query:
            found = await run(recgov.search, query, limit)
        else:
            raise ToolError("Give a query, or latitude and longitude")
        return [campground_dict(c) for c in found if c.reservable]

    @mcp.tool
    async def find_open_campsites(
        arrival_from: Dates,
        nights: Annotated[int, Field(ge=1, le=21)],
        arrival_to: Annotated[
            date | None, Field(description="Last arrival date; omit for exact dates")
        ] = None,
        campground_ids: list[str] | None = None,
        query: Annotated[
            str | None, Field(description="Search campgrounds by text instead")
        ] = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_miles: Annotated[float, Field(gt=0, le=300)] = 50,
        max_campgrounds: Annotated[int, Field(ge=1, le=30)] = 10,
        arrival_weekdays: Weekdays = None,
        group_size: int | None = None,
        site_types: SiteTypes = None,
        include_group_sites: bool = False,
    ) -> dict[str, Any]:
        """What's bookable right now: every site free for the whole stay, at
        the given campgrounds, or the ones matching `query`, or the nearest
        to a point. Campgrounds with nothing open are listed by name only."""
        crit = criteria(
            arrival_from,
            arrival_to,
            nights,
            arrival_weekdays,
            group_size,
            site_types,
            None,
            include_group_sites,
        )
        if campground_ids:
            campgrounds = list(
                await asyncio.gather(
                    *(run(recgov.campground, cid) for cid in campground_ids)
                )
            )
        elif latitude is not None and longitude is not None:
            campgrounds = await run(
                recgov.search_near, latitude, longitude, radius_miles, max_campgrounds
            )
        elif query:
            campgrounds = await run(recgov.search, query, max_campgrounds)
        else:
            raise ToolError("Give campground_ids, a query, or latitude and longitude")
        campgrounds = [c for c in campgrounds if c.reservable][:max_campgrounds]
        results = await run(service.find_open, recgov, campgrounds, crit, today())
        open_at, nothing, errors, cards = [], [], [], []
        for r in results:
            if r.error:
                errors.append({"name": r.campground.name, "error": r.error})
            elif r.stays:
                open_at.append(
                    {
                        **campground_dict(r.campground),
                        "open_count": len(r.stays),
                        "stays": [stay_dict(s) for s in r.stays[:STAYS_PER_CAMPGROUND]],
                    }
                )
                # The watcher's alert, as if a watch had found these.
                alert = service.Alert(
                    watch_id=None,
                    watch_name=f"Open now · {nights} night{'s' if nights != 1 else ''}",
                    campground_name=r.campground.name,
                    stays=[
                        service.OpenStay(
                            offer(service.HoldTarget(s, r.campground.name)), s
                        )
                        for s in r.stays[: messages.ALERT_LIST_MAX]
                    ],
                    auto_hold=False,
                )
                rows = messages.alert_buttons(alert, prefix=PREFIX)
                cards.append(card(messages.alert(alert), rows))
            else:
                nothing.append(r.campground.name)
        return {
            "open": open_at,
            "nothing_open": nothing,
            "errors": errors,
            "telegram": cards,
        }

    @mcp.tool
    async def create_watch(
        campground_id: str,
        arrival_from: Dates,
        nights: Annotated[int, Field(ge=1, le=21)],
        arrival_to: Annotated[
            date | None, Field(description="Last arrival date; omit for exact dates")
        ] = None,
        campsite_ids: Annotated[
            list[str] | None, Field(description="Only these sites; omit for any")
        ] = None,
        arrival_weekdays: Weekdays = None,
        group_size: int | None = None,
        site_types: SiteTypes = None,
        include_group_sites: bool = False,
        auto_hold: Annotated[
            bool,
            Field(
                description="Put the first opening straight in the cart (once), "
                "rather than only alerting; recreation.gov only"
            ),
        ] = False,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Watch a campground for cancellations: the watcher checks every
        minute or so and alerts on Telegram with Hold buttons."""
        crit = criteria(
            arrival_from,
            arrival_to,
            nights,
            arrival_weekdays,
            group_size,
            site_types,
            campsite_ids,
            include_group_sites,
        )
        if crit.arrival_to < today():
            raise ToolError("Invalid: every arrival date has passed")
        campground = await run(recgov.campground, campground_id)
        watch = await run(
            service.create_watch,
            sessions,
            campground,
            crit,
            name=name,
            auto_hold=auto_hold,
        )
        out = watch_dict(watch)
        if is_state_park(campground_id):
            return out  # no release schedule: checked as usual
        try:
            release = parse_releases(
                await run(recgov.releases, campground_id), utcnow()
            )
        except ToolError:
            return out  # the watch is made; the schedule is a nicety
        out["on_sale_through"] = release.open_through.isoformat()
        if waiting_on(crit, release, today()):
            out["on_sale_at"] = release.next_at.isoformat()
        elif crit.arrival_to > release.open_through:
            out["note"] = (
                "Some arrival dates go on sale after the next release; the "
                "watcher arms for them when they're next."
            )
        return out

    @mcp.tool
    async def set_watch_auto_pay(watch_id: int, on: bool = True) -> dict[str, Any]:
        """Pay for the watch's automatic hold too, with the card saved on the
        recreation.gov account (turns auto_hold on). Paying also needs the
        operator's CAMPSITE_SUBMIT_PAYMENT; without it it's a dry run."""
        out = watch_dict(await run(service.set_auto_pay, sessions, watch_id, on))
        out["payment_enabled"] = settings.submit_payment
        return out

    @mcp.tool
    async def list_watches(include_ended: bool = False) -> list[dict[str, Any]]:
        """The watches, soonest arrival first."""
        return [
            watch_dict(w)
            for w in await run(service.list_watches, sessions, include_ended)
        ]

    @mcp.tool
    async def stop_watch(watch_id: int) -> dict[str, Any]:
        """Stop alerting for a watch (it's kept, and can be resumed)."""
        return watch_dict(await run(service.stop_watch, sessions, watch_id))

    @mcp.tool
    async def resume_watch(watch_id: int) -> dict[str, Any]:
        """Start a stopped watch again."""
        return watch_dict(await run(service.resume_watch, sessions, watch_id))

    @mcp.tool
    async def hold_campsite(
        campground_id: str,
        campsite_id: str,
        check_in: Dates,
        nights: Annotated[int, Field(ge=1, le=21)],
        pay: Annotated[
            bool, Field(description="Go on to pay with the saved card")
        ] = False,
        group_size: Annotated[
            int | None, Field(ge=1, description="For the order, when paying")
        ] = None,
    ) -> dict[str, Any]:
        """Put one site in the recreation.gov cart for these dates, signed in
        as the account owner. Holds it for 15 minutes; the person pays on
        recreation.gov (`pay_at`), or with `pay` it pays now (a dry run unless
        payment is enabled: see `checkout_status`). Takes up to a minute or
        two (a real browser). A site and dates already in the cart (or paid
        for) aren't held again: that hold comes back instead. `challenged`
        means recreation.gov wanted a human check, so it isn't in the cart.
        recreation.gov only: a state park is booked on ReserveCalifornia."""
        if is_state_park(campground_id):
            raise ToolError(service.NO_STATE_PARK_HOLDS)
        campground = await run(recgov.campground, campground_id)
        stay = await run(
            service.stay_at, recgov, campground_id, campsite_id, check_in, nights
        )
        target = service.HoldTarget(stay, campground.name)
        return await hold_target(
            target, (group_size or settings.group_size) if pay else None
        )

    @mcp.tool
    async def pay_hold(hold_id: int) -> dict[str, Any]:
        """Pay for an earlier hold that's still in the cart, with the card
        saved on the account (a dry run unless payment is enabled). A
        `challenged` hold (recreation.gov wanted a human check) can be paid
        once the user has added the site on recreation.gov themselves; it
        stops if the cart doesn't have it."""
        return await pay(hold_id)

    @mcp.tool
    async def ask_auto_pay(watch_id: int) -> dict[str, Any]:
        """The card asking the person to let a watch pay for its automatic
        hold (`telegram`, to send as given). Their tap turns it on."""
        watch = await run(service.get_watch, sessions, watch_id)
        text = messages.ask_auto_pay(watch, settings.submit_payment)
        return {"telegram": card(text, messages.ask_auto_pay_buttons(watch_id, PREFIX))}

    @mcp.tool
    async def press_button(
        data: Annotated[str, Field(description="The tapped button's data, camp:…")],
    ) -> dict[str, Any]:
        """Do what a tapped `camp:` button does, as the watcher's bot would:
        hold a site, pay for a hold, turn on a watch's auto-pay, or stop a
        watch. Send `telegram` as given. Holding or paying takes a minute or
        two (a real browser)."""
        action, _, raw_id = data.removeprefix(PREFIX).partition(":")
        if action == "none":
            return {"note": "Nothing to do; the person said no."}
        if not raw_id.isdigit():
            raise ToolError(f"Not a campsite button: {data}")
        key = int(raw_id)
        if action == "hold":
            target = found.get(key)
            if target is None:
                return {"telegram": card("That opening is no longer on record.")}
            return await hold_target(target, None)
        if action == "pay":
            return await pay(key)
        if action == "autopay":
            watch = await run(service.set_auto_pay, sessions, key, True)
            done = (
                "Auto-pay is on"
                if settings.submit_payment
                else "Auto-pay is on (dry run)"
            )
            return {
                "telegram": card(f"{done} for #{watch.id}."),
                **watch_dict(watch),
            }
        if action in ("stop", "booked"):
            reason = BOOKED if action == "booked" else STOPPED
            watch = await run(service.stop_watch, sessions, key, reason)
            text = (
                "Booked — stopped watching."
                if action == "booked"
                else "Stopped watching."
            )
            return {"telegram": card(f"{text} (#{watch.id})"), **watch_dict(watch)}
        raise ToolError(f"Not a campsite button: {data}")

    @mcp.tool
    async def alert_timing(
        days: Annotated[int, Field(ge=1, le=365)] = 30,
    ) -> dict[str, Any]:
        """The watcher against recreation.gov's own alert emails: for each
        alert email, whether the watcher saw the opening before the email
        arrived (`watcher_first`, `lead_seconds` > 0) or the email did
        (`email_first`), plus the medians."""
        summary = await run(
            service.timing_summary, sessions, utcnow() - timedelta(days=days)
        )
        return {
            "counts": summary.counts,
            "median_watcher_lead_seconds": summary.median_lead_s,
            "median_recgov_to_inbox_seconds": summary.median_email_delay_s,
            "emails": [
                {
                    "received_at": r.received_at.isoformat(),
                    "available_as_of": (
                        r.available_as_of.isoformat() if r.available_as_of else None
                    ),
                    "campground": r.campground_name,
                    "sites": r.sites,
                    "watcher_saw_at": (
                        r.watcher_seen_at.isoformat() if r.watcher_seen_at else None
                    ),
                    "lead_seconds": r.lead_seconds,
                    "outcome": r.outcome,
                    "found_by_emails_check": bool(r.triggered_finds),
                }
                for r in summary.rows[:50]
            ],
        }

    @mcp.tool
    async def list_holds(
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict[str, Any]]:
        """Recent holds, newest first."""
        return [hold_dict(h) for h in await run(service.list_holds, sessions, limit)]

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
