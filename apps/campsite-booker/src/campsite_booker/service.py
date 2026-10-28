"""The app's operations, shared by the watcher, the MCP tools and the CLI.

Searching (`find_open`) needs only recreation.gov's public API. Watches are
rows; `check_watches` fetches each watched campground once, records what's
newly free as `Opening`s and returns them, and the watcher sends them on.
Holding goes through `booker.hold` (a real browser, signed in) and is
recorded with `record_hold`.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from campsite_booker.alerts import parse_alert
from campsite_booker.availability import StayCriteria, find_stays
from campsite_booker.checkout import CheckoutResult
from campsite_booker.db import (
    BY_EMAIL,
    BY_POLL,
    EMAIL_FIRST,
    EXPIRED,
    HELD,
    MISSED,
    NOT_WATCHED,
    PAID,
    STOPPED,
    UNKNOWN,
    WATCHER_FIRST,
    AlertEmail,
    Hold,
    Opening,
    Watch,
)
from campsite_booker.recgov import Campground, Campsite, RecGov, RecGovError, Stay
from campsite_booker.releases import Release, parse_releases, waiting_on

# recreation.gov's cart keeps a site for 15 minutes after "Add to Cart".
CART_HOLD = timedelta(minutes=15)
# Campgrounds fetched at once by a search across many.
SEARCH_WORKERS = 4


class NotFound(LookupError):
    pass


# --- searching ------------------------------------------------------------------


@dataclass(frozen=True)
class CampgroundStays:
    campground: Campground
    stays: list[Stay]
    # Set when this campground's availability couldn't be read.
    error: str | None = None


def find_open(
    recgov: RecGov,
    campgrounds: Iterable[Campground],
    criteria: StayCriteria,
    today: date,
) -> list[CampgroundStays]:
    """What's bookable right now at each campground, in the order given.
    One campground failing doesn't sink the rest; it carries the error."""
    campgrounds = list(campgrounds)
    arrivals = criteria.arrivals(today)
    if not arrivals:
        return [CampgroundStays(c, []) for c in campgrounds]

    def one(campground: Campground) -> CampgroundStays:
        try:
            sites = recgov.availability(
                campground.id,
                arrivals[0],
                arrivals[-1] + timedelta(days=criteria.nights - 1),
            )
        except RecGovError as e:
            return CampgroundStays(campground, [], str(e))
        return CampgroundStays(
            campground, find_stays(campground.id, sites.values(), criteria, today)
        )

    with ThreadPoolExecutor(SEARCH_WORKERS) as pool:
        return list(pool.map(one, campgrounds))


# --- watches --------------------------------------------------------------------


def create_watch(
    sessions: sessionmaker[Session],
    campground: Campground,
    criteria: StayCriteria,
    *,
    name: str | None = None,
    auto_hold: bool = False,
) -> Watch:
    with sessions.begin() as session:
        watch = Watch(
            name=name or default_name(campground, criteria),
            campground_id=campground.id,
            campground_name=campground.name,
            campsite_ids=sorted(criteria.campsite_ids),
            arrival_from=criteria.arrival_from,
            arrival_to=criteria.arrival_to,
            nights=criteria.nights,
            arrival_weekdays=sorted(criteria.arrival_weekdays),
            group_size=criteria.group_size,
            site_types=list(criteria.site_types),
            include_group_sites=criteria.include_group_sites,
            auto_hold=auto_hold,
        )
        session.add(watch)
        session.flush()
        return watch


def default_name(campground: Campground, criteria: StayCriteria) -> str:
    start = criteria.arrival_from.strftime("%b %-d")
    if criteria.arrival_from == criteria.arrival_to:
        dates = start
    else:
        dates = f"{start}–{criteria.arrival_to.strftime('%b %-d')}"
    nights = f"{criteria.nights} night{'s' if criteria.nights != 1 else ''}"
    return f"{campground.name}, {dates}, {nights}"


def list_watches(
    sessions: sessionmaker[Session], include_ended: bool = False
) -> list[Watch]:
    with sessions() as session:
        query = select(Watch).order_by(Watch.arrival_from, Watch.id)
        if not include_ended:
            query = query.where(Watch.active.is_(True))
        return list(session.scalars(query))


def get_watch(sessions: sessionmaker[Session], watch_id: int) -> Watch:
    with sessions() as session:
        watch = session.get(Watch, watch_id)
        if watch is None:
            raise NotFound(f"No watch {watch_id}")
        return watch


def stop_watch(
    sessions: sessionmaker[Session], watch_id: int, reason: str = STOPPED
) -> Watch:
    with sessions.begin() as session:
        watch = session.get(Watch, watch_id)
        if watch is None:
            raise NotFound(f"No watch {watch_id}")
        watch.active = False
        watch.ended_reason = reason
        return watch


def resume_watch(sessions: sessionmaker[Session], watch_id: int) -> Watch:
    with sessions.begin() as session:
        watch = session.get(Watch, watch_id)
        if watch is None:
            raise NotFound(f"No watch {watch_id}")
        watch.active = True
        watch.ended_reason = None
        return watch


# --- checking watches -------------------------------------------------------------


@dataclass(frozen=True)
class OpenStay:
    """A stay a watch found, by the `Opening` row that records it."""

    opening_id: int
    stay: Stay


@dataclass(frozen=True)
class Alert:
    watch_id: int
    watch_name: str
    campground_name: str
    stays: list[OpenStay]
    # The watch wants the first of these held now (and hasn't had its one
    # automatic hold yet).
    auto_hold: bool
    # ...and paid for, with this group size on the order.
    auto_pay: bool = False
    group_size: int | None = None
    # What kind of check found them (BY_POLL, BY_RELEASE, BY_EMAIL).
    found_by: str = BY_POLL


@dataclass(frozen=True)
class Check:
    alerts: list[Alert]
    # What each campground fetched showed, by campsite id.
    sites: dict[str, dict[str, Campsite]]


def check_watches(
    sessions: sessionmaker[Session],
    recgov: RecGov,
    now: datetime,
    today: date,
) -> list[Alert]:
    """Check every active watch against recreation.gov and return what's
    newly free (or free again) for each, oldest arrival first.

    Each campground is fetched once per check, however many watches it has.
    Watches whose arrival dates have all passed are ended. A campground that
    can't be read leaves its watches' openings as they were and sets their
    `last_error`.
    """
    return check_campgrounds(sessions, recgov, now, today).alerts


def check_campgrounds(
    sessions: sessionmaker[Session],
    recgov: RecGov,
    now: datetime,
    today: date,
    only: Collection[str] | None = None,
    found_by: str = BY_POLL,
) -> Check:
    """`check_watches` for just the campgrounds in `only` (all when None),
    also returning what was fetched: a release burst checks one campground
    every couple of seconds and needs to see when the release lands.
    `found_by` is recorded on what's new, for timing the triggers."""
    with sessions.begin() as session:
        watches = list(session.scalars(select(Watch).where(Watch.active.is_(True))))
        for watch in watches:
            if watch.arrival_to < today:
                watch.active = False
                watch.ended_reason = EXPIRED
        watches = [w for w in watches if w.active]

        by_campground: dict[str, list[Watch]] = defaultdict(list)
        for watch in watches:
            if only is not None and watch.campground_id not in only:
                continue
            if watch.criteria.arrivals(today):
                by_campground[watch.campground_id].append(watch)

        alerts = []
        fetched: dict[str, dict[str, Campsite]] = {}
        for campground_id, group in by_campground.items():
            first = min(w.criteria.arrivals(today)[0] for w in group)
            last = max(w.criteria.last_night for w in group)
            try:
                sites = recgov.availability(campground_id, first, last)
            except RecGovError as e:
                logger.warning(f"Checking campground {campground_id} failed: {e}")
                for watch in group:
                    watch.last_error = str(e)
                continue
            fetched[campground_id] = sites
            for watch in group:
                stays = find_stays(campground_id, sites.values(), watch.criteria, today)
                fresh = _reconcile(session, watch, stays, now, found_by)
                watch.last_checked_at = now
                watch.last_error = None
                if fresh:
                    alerts.append(
                        Alert(
                            watch_id=watch.id,
                            watch_name=watch.name,
                            campground_name=watch.campground_name,
                            stays=fresh,
                            auto_hold=watch.auto_hold and watch.auto_held_at is None,
                            auto_pay=watch.auto_pay,
                            group_size=watch.group_size,
                            found_by=found_by,
                        )
                    )
                    for item in fresh:
                        logger.info(
                            f"OPENING watch={watch.id} campground={campground_id} "
                            f"campsite={item.stay.campsite_id} site={item.stay.site} "
                            f"check_in={item.stay.check_in} found_by={found_by} "
                            f"at={now.isoformat()}"
                        )
        return Check(alerts, fetched)


def _reconcile(
    session: Session,
    watch: Watch,
    stays: list[Stay],
    now: datetime,
    found_by: str = BY_POLL,
) -> list[OpenStay]:
    """Bring the watch's openings in line with `stays`; return the ones that
    are new or back after being gone."""
    existing = {
        (o.campsite_id, o.check_in, o.nights): o
        for o in session.scalars(select(Opening).where(Opening.watch_id == watch.id))
    }
    fresh = []
    seen = set()
    for stay in stays:
        key = (stay.campsite_id, stay.check_in, stay.nights)
        seen.add(key)
        opening = existing.get(key)
        if opening is None:
            opening = Opening(
                watch_id=watch.id,
                campsite_id=stay.campsite_id,
                site=stay.site,
                loop=stay.loop,
                campsite_type=stay.campsite_type,
                max_people=stay.max_people,
                check_in=stay.check_in,
                nights=stay.nights,
                first_seen_at=now,
                opened_at=now,
                found_by=found_by,
                last_seen_at=now,
            )
            session.add(opening)
            session.flush()
            fresh.append(OpenStay(opening.id, stay))
        else:
            opening.last_seen_at = now
            if opening.gone_at is not None:
                opening.gone_at = None
                opening.notified_at = None
                opening.opened_at = now
                opening.found_by = found_by
                fresh.append(OpenStay(opening.id, stay))
    for key, opening in existing.items():
        if key not in seen and opening.gone_at is None:
            opening.gone_at = now
    return fresh


def mark_notified(
    sessions: sessionmaker[Session], opening_ids: Iterable[int], now: datetime
) -> None:
    with sessions.begin() as session:
        for opening in session.scalars(
            select(Opening).where(Opening.id.in_(list(opening_ids)))
        ):
            opening.notified_at = now


def unsent_alerts(sessions: sessionmaker[Session]) -> list[Alert]:
    """Openings still free that were found but never delivered (Telegram was
    down when they turned up). The watcher sends these before checking
    again, so nothing found is lost to a failed send."""
    with sessions() as session:
        rows = session.scalars(
            select(Opening)
            .join(Watch)
            .where(
                Watch.active.is_(True),
                Opening.notified_at.is_(None),
                Opening.gone_at.is_(None),
            )
            .order_by(Opening.watch_id, Opening.check_in, Opening.id)
        )
        grouped: dict[int, list[Opening]] = defaultdict(list)
        for opening in rows:
            grouped[opening.watch_id].append(opening)
        return [
            Alert(
                watch_id=watch_id,
                watch_name=openings[0].watch.name,
                campground_name=openings[0].watch.campground_name,
                stays=[OpenStay(o.id, opening_stay(o)) for o in openings],
                # The automatic hold is only for what's just turned up.
                auto_hold=False,
            )
            for watch_id, openings in grouped.items()
        ]


def opening_stay(opening: Opening) -> Stay:
    return Stay(
        campground_id=opening.watch.campground_id,
        campsite_id=opening.campsite_id,
        site=opening.site,
        loop=opening.loop,
        campsite_type=opening.campsite_type,
        max_people=opening.max_people,
        check_in=opening.check_in,
        nights=opening.nights,
    )


@dataclass(frozen=True)
class HoldTarget:
    """What to put in the cart, and the watch it's for, if any."""

    stay: Stay
    campground_name: str
    watch_id: int | None = None


def hold_target(sessions: sessionmaker[Session], opening_id: int) -> HoldTarget:
    with sessions() as session:
        opening = session.get(Opening, opening_id)
        if opening is None:
            raise NotFound(f"No opening {opening_id}")
        return HoldTarget(
            opening_stay(opening), opening.watch.campground_name, opening.watch_id
        )


def stay_at(
    recgov: RecGov, campground_id: str, campsite_id: str, check_in: date, nights: int
) -> Stay:
    """The `Stay` for one site and dates, with the site's details filled in
    from recreation.gov. Doesn't check it's free: the hold finds that out."""
    sites = recgov.availability(
        campground_id, check_in, check_in + timedelta(days=nights - 1)
    )
    site = sites.get(str(campsite_id))
    if site is None:
        raise NotFound(f"No site {campsite_id} at campground {campground_id}")
    return Stay(
        campground_id=campground_id,
        campsite_id=site.campsite_id,
        site=site.site,
        loop=site.loop,
        campsite_type=site.campsite_type,
        max_people=site.max_people,
        check_in=check_in,
        nights=nights,
    )


def claim_auto_hold(
    sessions: sessionmaker[Session], watch_id: int, now: datetime
) -> bool:
    """Take the watch's one automatic hold. False if it's been used (or the
    watch has stopped) since the alert was made."""
    with sessions.begin() as session:
        watch = session.get(Watch, watch_id)
        if watch is None or not watch.active or watch.auto_held_at is not None:
            return False
        watch.auto_held_at = now
        return True


# --- holds ------------------------------------------------------------------------


def record_hold(
    sessions: sessionmaker[Session],
    target: HoldTarget,
    status: str,
    detail: str | None,
    page_url: str | None,
    now: datetime,
    automatic: bool = False,
    checkout: CheckoutResult | None = None,
) -> Hold:
    with sessions.begin() as session:
        hold = Hold(
            watch_id=target.watch_id,
            campground_id=target.stay.campground_id,
            campground_name=target.campground_name,
            campsite_id=target.stay.campsite_id,
            site=target.stay.site,
            check_in=target.stay.check_in,
            check_out=target.stay.check_out,
            status=status,
            detail=detail,
            page_url=page_url,
            automatic=automatic,
            created_at=now,
            expires_at=now + CART_HOLD if status == HELD else None,
        )
        if checkout is not None:
            _apply_checkout(hold, checkout)
        session.add(hold)
        session.flush()
        return hold


def live_hold(
    sessions: sessionmaker[Session], stay: Stay, now: datetime
) -> Hold | None:
    """The hold that has `stay` in the cart (still inside its 15 minutes) or
    paid for, if any: holding it again would only fail or add it twice."""
    with sessions() as session:
        holds = session.scalars(
            select(Hold).where(
                Hold.campground_id == stay.campground_id,
                Hold.campsite_id == stay.campsite_id,
                Hold.check_in == stay.check_in,
                Hold.check_out == stay.check_out,
                Hold.status == HELD,
            )
        )
        return next(
            (
                h
                for h in holds
                if h.checkout_status == PAID
                or (h.expires_at is not None and h.expires_at > now)
            ),
            None,
        )


def held_openings(
    sessions: sessionmaker[Session], stays: Iterable[OpenStay], now: datetime
) -> set[int]:
    """The opening ids among `stays` that have a live hold."""
    return {s.opening_id for s in stays if live_hold(sessions, s.stay, now)}


def _apply_checkout(hold: Hold, checkout: CheckoutResult) -> None:
    hold.checkout_status = checkout.status
    hold.checkout_detail = checkout.detail
    hold.confirmation = checkout.confirmation
    if checkout.status == PAID:
        hold.expires_at = None


def get_hold(sessions: sessionmaker[Session], hold_id: int) -> Hold:
    with sessions() as session:
        hold = session.get(Hold, hold_id)
        if hold is None:
            raise NotFound(f"No hold {hold_id}")
        return hold


def record_checkout(
    sessions: sessionmaker[Session], hold_id: int, checkout: CheckoutResult
) -> Hold:
    """How paying for an earlier hold went (the Pay button, `pay_hold`)."""
    with sessions.begin() as session:
        hold = session.get(Hold, hold_id)
        if hold is None:
            raise NotFound(f"No hold {hold_id}")
        _apply_checkout(hold, checkout)
        return hold


def pay_refusal(hold: Hold) -> str | None:
    """Why paying for `hold` mustn't be tried, or None if it may. Paying
    starts from the cart when there's no order page, so a hold that never
    got into the cart would pay for whatever else is there; and after an
    `UNKNOWN` checkout (Confirm was pressed) it may already be paid."""
    if hold.checkout_status == PAID:
        return "That one's already paid."
    if hold.status != HELD:
        return "That hold didn't put the site in the cart, so there's nothing to pay."
    if hold.checkout_status == UNKNOWN:
        return (
            "The last try pressed Confirm and may have paid; check your "
            "reservations on recreation.gov before paying again."
        )
    return None


def hold_group_size(sessions: sessionmaker[Session], hold: Hold, default: int) -> int:
    """The group size to put on a hold's order: its watch's, or `default`."""
    if hold.watch_id is None:
        return default
    with sessions() as session:
        watch = session.get(Watch, hold.watch_id)
        return (watch.group_size if watch else None) or default


def set_auto_pay(sessions: sessionmaker[Session], watch_id: int, on: bool) -> Watch:
    """Pay for the watch's automatic hold too (turning it on turns
    `auto_hold` on: there's nothing to pay for without a hold)."""
    with sessions.begin() as session:
        watch = session.get(Watch, watch_id)
        if watch is None:
            raise NotFound(f"No watch {watch_id}")
        watch.auto_pay = on
        if on:
            watch.auto_hold = True
        return watch


# --- releases ---------------------------------------------------------------------


@dataclass(frozen=True)
class Waiting:
    """A watch whose arrival dates the next release puts on sale."""

    watch_id: int
    watch_name: str
    campground_id: str
    campground_name: str
    release_at: datetime
    # Already announced for this release.
    announced: bool


def waiting_on_releases(
    sessions: sessionmaker[Session],
    recgov: RecGov,
    now: datetime,
    today: date,
    cache: dict[str, tuple[datetime, Release]],
    refresh: timedelta = timedelta(hours=6),
) -> list[Waiting]:
    """Watches waiting on their campground's next release. Release schedules
    are fetched only for campgrounds with a watch past the open window, and
    cached in `cache` (campground id → (fetched at, release)) until
    `refresh` has passed or the release has happened."""
    waiting = []
    for watch in list_watches(sessions):
        criteria = watch.criteria
        cached = cache.get(watch.campground_id)
        if cached is not None:
            fetched_at, release = cached
            stale = now - fetched_at > refresh or (
                release.next_at is not None and release.next_at <= now
            )
            if stale:
                cached = None
        if cached is None:
            try:
                release = parse_releases(recgov.releases(watch.campground_id), now)
            except RecGovError as e:
                logger.warning(f"Release schedule for {watch.campground_id}: {e}")
                continue
            cache[watch.campground_id] = (now, release)
        else:
            release = cached[1]
        if release.next_at and waiting_on(criteria, release, today):
            waiting.append(
                Waiting(
                    watch.id,
                    watch.name,
                    watch.campground_id,
                    watch.campground_name,
                    release.next_at,
                    watch.armed_release_at == release.next_at,
                )
            )
    return waiting


def mark_announced(
    sessions: sessionmaker[Session], watch_ids: Iterable[int], release_at: datetime
) -> None:
    with sessions.begin() as session:
        for watch in session.scalars(
            select(Watch).where(Watch.id.in_(list(watch_ids)))
        ):
            watch.armed_release_at = release_at


def list_holds(sessions: sessionmaker[Session], limit: int = 10) -> list[Hold]:
    with sessions() as session:
        return list(
            session.scalars(
                select(Hold)
                .order_by(Hold.created_at.desc(), Hold.id.desc())
                .limit(limit)
            )
        )


# --- recreation.gov's alert emails ------------------------------------------------

# An alert email no opening turned up for in this long counts as missed.
EMAIL_MATCH_WINDOW = timedelta(minutes=30)
# How far before the email an opening can have opened and still be the one
# it's about (the watcher may have seen it a while before recreation.gov
# emailed).
EMAIL_LOOKBACK = timedelta(hours=6)


class Mailbox(Protocol):
    def recent_ids(self, days: int = 1) -> list[str]: ...

    def fetch(self, gmail_id: str) -> Any: ...


def record_alert_emails(
    sessions: sessionmaker[Session],
    mailbox: Mailbox,
    recgov: RecGov,
    tz: ZoneInfo,
    now: datetime,
) -> list[AlertEmail]:
    """Every alert email from the last day not seen before, recorded and
    returned oldest first. An email that doesn't read as an alert is
    recorded too (so it isn't fetched again), with no sites."""
    ids = mailbox.recent_ids(1)
    with sessions() as session:
        known = set(
            session.scalars(
                select(AlertEmail.gmail_id).where(AlertEmail.gmail_id.in_(ids))
            )
        )
    new = sorted(
        (mailbox.fetch(gmail_id) for gmail_id in ids if gmail_id not in known),
        key=lambda m: m.received_at,
    )
    recorded = []
    for message in new:
        alert = parse_alert(message.html, tz)
        campground_id = None
        if alert is not None:
            try:
                campground_id = recgov.campground_of(alert.campsite_ids[0])
            except RecGovError as e:
                logger.warning(f"Alert email {message.gmail_id}: {e}")
        else:
            logger.warning(f"Alert email {message.gmail_id} didn't read as an alert")
        row = AlertEmail(
            gmail_id=message.gmail_id,
            received_at=message.received_at,
            available_as_of=alert.available_as_of if alert else None,
            campground_name=alert.campground_name if alert else None,
            campground_id=campground_id,
            campsite_ids=alert.campsite_ids if alert else [],
            sites=alert.sites if alert else [],
            start_date=alert.start_date if alert else None,
            end_date=alert.end_date if alert else None,
            other_recipients=alert.other_recipients if alert else None,
            processed_at=now,
            outcome=None if alert else MISSED,
        )
        with sessions.begin() as session:
            session.add(row)
        logger.info(
            f"ALERT_EMAIL gmail_id={row.gmail_id} campground={row.campground_id} "
            f"campsites={','.join(row.campsite_ids)} "
            f"available_as_of={_iso(row.available_as_of)} "
            f"received_at={row.received_at.isoformat()} processed_at={now.isoformat()} "
            f"others={row.other_recipients}"
        )
        recorded.append(row)
    return recorded


def record_triggered_finds(
    sessions: sessionmaker[Session], gmail_id: str, finds: int
) -> None:
    with sessions.begin() as session:
        row = session.get(AlertEmail, gmail_id)
        if row is not None:
            row.triggered_finds = finds


def score_alert_emails(
    sessions: sessionmaker[Session], now: datetime
) -> list[AlertEmail]:
    """Decide, for each alert email not yet decided, who saw the opening
    first: the watcher or the email. Returns the ones decided now.

    The opening is the one for the email's site(s) at that campground that
    opened closest before the email (or, if the watcher only saw it after,
    the first after). Its `found_by` says whether the email's own check is
    what found it.
    """
    decided = []
    with sessions.begin() as session:
        pending = session.scalars(
            select(AlertEmail).where(AlertEmail.outcome.is_(None))
        ).all()
        for email in pending:
            watched = session.scalar(
                select(Watch.id).where(
                    Watch.active.is_(True), Watch.campground_id == email.campground_id
                )
            )
            opening = _matching_opening(session, email)
            if opening is not None:
                opened = opening.opened_at or opening.first_seen_at
                lead = (email.received_at - opened).total_seconds()
                email.opening_id = opening.id
                email.watcher_seen_at = opened
                email.lead_seconds = lead
                email.outcome = (
                    WATCHER_FIRST
                    if lead > 0 and opening.found_by != BY_EMAIL
                    else EMAIL_FIRST
                )
            elif watched is None:
                email.outcome = NOT_WATCHED
            elif now - email.received_at > EMAIL_MATCH_WINDOW:
                email.outcome = MISSED
            else:
                continue
            logger.info(timing_line(email))
            decided.append(email)
    return decided


def _matching_opening(session: Session, email: AlertEmail) -> Opening | None:
    if not email.campground_id or not email.campsite_ids:
        return None
    candidates = session.scalars(
        select(Opening)
        .join(Watch)
        .where(
            Watch.campground_id == email.campground_id,
            Opening.campsite_id.in_(email.campsite_ids),
            Opening.opened_at >= email.received_at - EMAIL_LOOKBACK,
            Opening.opened_at <= email.received_at + EMAIL_MATCH_WINDOW,
        )
    ).all()
    before = [o for o in candidates if o.opened_at <= email.received_at]
    if before:
        return max(before, key=lambda o: o.opened_at)
    after = [o for o in candidates if o.opened_at > email.received_at]
    return min(after, key=lambda o: o.opened_at) if after else None


def _iso(moment: datetime | None) -> str:
    return moment.isoformat() if moment else "-"


def timing_line(email: AlertEmail) -> str:
    """One log line per decided email, `key=value`, for grepping the log."""

    def seconds(value: float | None) -> str:
        return "-" if value is None else str(round(value))

    email_delay = (
        (email.received_at - email.available_as_of).total_seconds()
        if email.available_as_of
        else None
    )
    return (
        f"TIMING outcome={email.outcome} gmail_id={email.gmail_id} "
        f"campground={email.campground_id} campsites={','.join(email.campsite_ids)} "
        f"sites={','.join(email.sites)} "
        f"recgov_as_of={_iso(email.available_as_of)} "
        f"email_received={email.received_at.isoformat()} "
        f"watcher_opened={_iso(email.watcher_seen_at)} "
        f"watcher_lead_s={seconds(email.lead_seconds)} "
        f"email_delay_s={seconds(email_delay)} "
        f"email_check_finds={email.triggered_finds}"
    )


@dataclass(frozen=True)
class TimingSummary:
    counts: dict[str, int]
    # Over emails where both saw it: how far ahead the watcher was.
    median_lead_s: float | None
    # Over all: from recreation.gov's "as of" to the email reaching Gmail.
    median_email_delay_s: float | None
    rows: list[AlertEmail]


def timing_summary(sessions: sessionmaker[Session], since: datetime) -> TimingSummary:
    with sessions() as session:
        rows = list(
            session.scalars(
                select(AlertEmail)
                .where(AlertEmail.received_at >= since)
                .order_by(AlertEmail.received_at.desc())
            )
        )
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.outcome or "pending"] += 1
    leads = [r.lead_seconds for r in rows if r.lead_seconds is not None]
    delays = [
        (r.received_at - r.available_as_of).total_seconds()
        for r in rows
        if r.available_as_of
    ]
    return TimingSummary(
        dict(counts),
        median(leads) if leads else None,
        median(delays) if delays else None,
        rows,
    )
