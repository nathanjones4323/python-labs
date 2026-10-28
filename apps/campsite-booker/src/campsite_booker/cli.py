"""`campsite-booker-cli`: search and manage watches from a terminal.

    campsite-booker-cli campgrounds "upper pines"
    campsite-booker-cli campgrounds --near 34.28,-119.29 --radius 40
    campsite-booker-cli open 232447 --arrive 2026-11-06 --nights 2
    campsite-booker-cli open 232447 232450 --arrive 2026-11-01 --until 2026-11-30 \\
        --nights 2 --days fri,sat --people 4 --type tent
    campsite-booker-cli watch 232447 --arrive 2026-11-06 --nights 2 --auto-hold
    campsite-booker-cli watches
    campsite-booker-cli stop 3
    campsite-booker-cli watch 232447 --arrive 2027-03-19 --nights 2 --auto-pay
    campsite-booker-cli hold 232447 100 --arrive 2026-11-06 --nights 2
    campsite-booker-cli hold 232447 100 --arrive 2026-11-06 --nights 2 --pay
    campsite-booker-cli pay 7
    campsite-booker-cli timing --days 14

Paying (--pay, --auto-pay, pay) uses the card saved on the recreation.gov
account, and is a dry run unless CAMPSITE_SUBMIT_PAYMENT=true.
"""

import argparse
from datetime import UTC, date, datetime, timedelta

from campsite_booker import booker, service
from campsite_booker.availability import StayCriteria, parse_weekdays
from campsite_booker.config import Settings
from campsite_booker.db import make_session_factory, migrate, utcnow
from campsite_booker.messages import site_kind
from campsite_booker.recgov import RecGov


def _criteria_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--arrive", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--until", type=date.fromisoformat, help="last arrival date (flexible)"
    )
    parser.add_argument("--nights", type=int, default=1)
    parser.add_argument("--days", help="arrival weekdays, e.g. fri,sat")
    parser.add_argument("--people", type=int)
    parser.add_argument("--type", action="append", help="site type word: tent, rv")
    parser.add_argument("--sites", help="only these campsite ids, comma-separated")
    parser.add_argument("--group-sites", action="store_true")


def _criteria(args: argparse.Namespace) -> StayCriteria:
    return StayCriteria(
        arrival_from=args.arrive,
        arrival_to=args.until or args.arrive,
        nights=args.nights,
        arrival_weekdays=parse_weekdays(args.days.split(",") if args.days else None),
        group_size=args.people,
        site_types=tuple(args.type or ()),
        campsite_ids=frozenset(args.sites.split(",")) if args.sites else frozenset(),
        include_group_sites=args.group_sites,
    )


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:+.0f}s"


def _clock(moment: datetime | None, tz, fmt: str) -> str:
    return moment.astimezone(tz).strftime(fmt) if moment else "-"


def _print_timing(summary: service.TimingSummary, tz, days: int) -> None:
    """One line per alert email: when recreation.gov says the site opened,
    when the email arrived, when the watcher saw it, and who won."""
    counts = ", ".join(f"{k} {n}" for k, n in sorted(summary.counts.items()))
    print(f"Last {days} days: {counts or 'no alert emails'}")
    print(
        f"Median watcher lead: {_seconds(summary.median_lead_s)}; median "
        f"recreation.gov -> inbox: {_seconds(summary.median_email_delay_s)}"
    )
    print(
        f"\n{'email received':<19}  {'campground':<26} {'sites':<8} "
        f"{'as of':<5}  {'watcher':<8}  {'lead':>6}  outcome"
    )
    for row in summary.rows:
        name = (row.campground_name or "?")[:26]
        print(
            f"{_clock(row.received_at, tz, '%Y-%m-%d %H:%M:%S')}  {name:<26} "
            f"{','.join(row.sites)[:8]:<8} "
            f"{_clock(row.available_as_of, tz, '%H:%M'):<5}  "
            f"{_clock(row.watcher_seen_at, tz, '%H:%M:%S'):<8}  "
            f"{_seconds(row.lead_seconds):>6}  {row.outcome or 'pending'}"
            + (" (the email's check found it)" if row.triggered_finds else "")
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="campsite-booker-cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("campgrounds", help="find campground ids")
    p.add_argument("query", nargs="?")
    p.add_argument("--near", help="lat,lng")
    p.add_argument("--radius", type=float, default=50)

    p = sub.add_parser("open", help="what's bookable now")
    p.add_argument("campground_ids", nargs="+")
    _criteria_args(p)

    p = sub.add_parser("watch", help="alert when a site opens up")
    p.add_argument("campground_id")
    _criteria_args(p)
    p.add_argument("--auto-hold", action="store_true")
    p.add_argument(
        "--auto-pay", action="store_true", help="also pay for the automatic hold"
    )
    p.add_argument("--name")

    p = sub.add_parser("watches", help="list watches")
    p.add_argument("--all", action="store_true", help="include ended ones")

    p = sub.add_parser("stop", help="stop a watch")
    p.add_argument("watch_id", type=int)

    p = sub.add_parser("hold", help="put a site in the cart now")
    p.add_argument("campground_id")
    p.add_argument("campsite_id")
    p.add_argument("--arrive", type=date.fromisoformat, required=True)
    p.add_argument("--nights", type=int, default=1)
    p.add_argument("--pay", action="store_true", help="go on to pay")
    p.add_argument("--people", type=int, help="group size for the order")

    p = sub.add_parser("pay", help="pay for an earlier hold")
    p.add_argument("hold_id", type=int)

    p = sub.add_parser(
        "timing", help="the watcher vs recreation.gov's alert emails, per email"
    )
    p.add_argument("--days", type=int, default=30)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    today = datetime.now(settings.timezone).date()

    with RecGov() as recgov:
        if args.command == "campgrounds":
            if args.near:
                lat, lng = (float(x) for x in args.near.split(","))
                found = recgov.search_near(lat, lng, args.radius)
            else:
                found = recgov.search(
                    args.query or parser.error("give a query or --near")
                )
            for c in found:
                miles = f"{c.distance_miles:5.1f} mi  " if c.distance_miles else ""
                print(f"{c.id:>9}  {miles}{c.name} ({c.park or c.state or ''})")
            return

        if args.command == "open":
            campgrounds = [recgov.campground(cid) for cid in args.campground_ids]
            for result in service.find_open(
                recgov, campgrounds, _criteria(args), today
            ):
                print(f"\n{result.campground.name} ({result.campground.id})")
                if result.error:
                    print(f"  error: {result.error}")
                for s in result.stays:
                    print(
                        f"  {s.check_in} → {s.check_out}  site {s.site:<6} "
                        f"id {s.campsite_id:<7} {site_kind(s.campsite_type)}, "
                        f"up to {s.max_people}  {s.url}"
                    )
                if not result.stays and not result.error:
                    print("  nothing open")
            return

        migrate(settings.database_url)
        sessions = make_session_factory(settings.database_url)

        if args.command == "watch":
            campground = recgov.campground(args.campground_id)
            watch = service.create_watch(
                sessions,
                campground,
                _criteria(args),
                name=args.name,
                auto_hold=args.auto_hold or args.auto_pay,
            )
            if args.auto_pay:
                service.set_auto_pay(sessions, watch.id, True)
            print(f"Watching #{watch.id}: {watch.name}")
        elif args.command == "watches":
            for w in service.list_watches(sessions, include_ended=args.all):
                state = "active" if w.active else f"ended ({w.ended_reason})"
                print(f"#{w.id:<4} {state:<18} {w.name}")
        elif args.command == "stop":
            print(f"Stopped #{service.stop_watch(sessions, args.watch_id).id}")
        elif args.command == "hold":
            campground = recgov.campground(args.campground_id)
            stay = service.stay_at(
                recgov, args.campground_id, args.campsite_id, args.arrive, args.nights
            )
            group_size = (args.people or settings.group_size) if args.pay else None
            result = booker.hold(settings, stay, group_size)
            hold = service.record_hold(
                sessions,
                service.HoldTarget(stay, campground.name),
                result.status,
                result.detail,
                result.page_url,
                utcnow(),
                checkout=result.checkout,
            )
            print(f"Hold #{hold.id} {result.status}: {result.detail}")
            if result.checkout:
                print(f"Checkout {result.checkout.status}: {result.checkout.detail}")
            if result.screenshot:
                print(f"Screenshot: {result.screenshot}")
        elif args.command == "timing":
            summary = service.timing_summary(
                sessions, datetime.now(UTC) - timedelta(days=args.days)
            )
            _print_timing(summary, settings.timezone, args.days)
        elif args.command == "pay":
            hold = service.get_hold(sessions, args.hold_id)
            refusal = service.pay_refusal(hold)
            if refusal:
                parser.exit(1, f"{refusal}\n")
            group_size = service.hold_group_size(sessions, hold, settings.group_size)
            checkout, screenshot = booker.pay(
                settings, hold.page_url, group_size, f"hold-{hold.id}"
            )
            service.record_checkout(sessions, hold.id, checkout)
            print(f"Checkout {checkout.status}: {checkout.detail}")
            if checkout.confirmation:
                print(f"Order {checkout.confirmation}")
            if screenshot:
                print(f"Screenshot: {screenshot}")


if __name__ == "__main__":
    main()
