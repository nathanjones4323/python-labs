"""Command line entry point: book a tee time, or just list what's available."""

import argparse
import uuid
from dataclasses import replace
from datetime import date, timezone

from tee_time_booker import worker
from tee_time_booker.config import Settings
from tee_time_booker.db import RunStatus, make_session_factory
from tee_time_booker.db.service import (
    claim_request,
    course_config,
    get_plan,
    list_courses,
    list_plans,
    plan_preferences,
    plan_target_date,
    recent_runs,
    request_for_plan,
)
from tee_time_booker.runner import find_tee_times


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tee-time-booker",
        description="Book a tee time on whichever site the course uses.",
    )
    parser.add_argument(
        "--plan",
        type=uuid.UUID,
        help="Booking plan to run (default: TEE_TIME_PLAN)",
    )
    parser.add_argument(
        "--request",
        type=uuid.UUID,
        help="Run this existing booking request instead of the plan's next date",
    )
    parser.add_argument(
        "--date",
        help="Book this YYYY-MM-DD instead of the plan's computed target date",
    )
    parser.add_argument(
        "--find-only",
        action="store_true",
        help="List the available tee times for the plan and exit without booking",
    )
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="Show the courses in the database and exit",
    )
    parser.add_argument(
        "--list-plans",
        action="store_true",
        help="Show every booking plan and exit",
    )
    parser.add_argument(
        "--runs",
        nargs="?",
        const=20,
        type=int,
        metavar="N",
        help="Show the last N runs (default 20) and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Book one tee time, record the run in the database, return an exit code."""
    args = parse_args(argv)

    # Command line arguments win over the environment for the same settings.
    settings = Settings.from_env()
    if args.plan:
        settings = replace(settings, plan_id=args.plan)
    if args.date:
        settings = replace(settings, target_date_override=date.fromisoformat(args.date))
    sessions = make_session_factory(settings.require_database_url())

    if args.list_courses:
        return _list_courses(sessions)
    if args.list_plans:
        return _list_plans(sessions)
    if args.runs is not None:
        return _list_runs(sessions, args.runs)

    if args.request:
        request_id = args.request
    else:
        if settings.plan_id is None:
            raise SystemExit("Pass --plan / TEE_TIME_PLAN or --request")
        with sessions() as session, session.begin():
            plan = get_plan(session, settings.plan_id)
            target = settings.target_date_override or plan_target_date(plan)
            if args.find_only:
                course = course_config(plan.course)
                criteria = plan_preferences(plan).criteria(target)
                env_prefix = plan.site_account.env_prefix
            else:
                request_id = request_for_plan(session, plan, target).id
        if args.find_only:
            tee_times = find_tee_times(
                course, criteria, settings, account_env_prefix=env_prefix
            )
            for tee_time in tee_times:
                fee = (
                    "" if tee_time.green_fee is None else f" ${tee_time.green_fee:.2f}"
                )
                side = f" {tee_time.side}" if tee_time.side else ""
                print(f"{tee_time.start:%Y-%m-%d %I:%M %p}{side}{fee}")
            return 0

    # Claim in one short transaction; the run itself may sleep for over an
    # hour waiting on a release, and must not hold a connection meanwhile.
    with sessions() as session, session.begin():
        claimed = claim_request(session, request_id)
    status = worker.execute(
        sessions,
        claimed,
        settings,
        # An override points at an already-open day; there's nothing to wait for.
        wait_for_release=settings.target_date_override is None,
    )
    return 0 if status in (RunStatus.SUCCESS, RunStatus.DRY_RUN) else 1


def _list_courses(sessions) -> int:
    with sessions() as session:
        for course in list_courses(session):
            print(f"{course.key:<12} {course.name} ({course.platform})")
    return 0


def _list_plans(sessions) -> int:
    with sessions() as session:
        for plan in list_plans(session):
            prefs = plan_preferences(plan)
            weekday = date(2024, 1, 1 + prefs.target_weekday).strftime("%A")
            print(
                f"{plan.id}  {plan.user.email:<28} {plan.name:<24} "
                f"{plan.course.key:<12} {weekday} +{prefs.weeks_ahead}w "
                f"{prefs.earliest:%H:%M}-{prefs.latest:%H:%M} "
                f"{prefs.holes}h x{prefs.players}"
                + ("" if plan.active else "  (inactive)")
            )
    return 0


def _list_runs(sessions, limit: int) -> int:
    with sessions() as session:
        for run in recent_runs(session, limit):
            request = run.request
            slot = f"{run.tee_time_local:%Y-%m-%d %H:%M}" if run.tee_time_local else "-"
            fee = f"${run.green_fee}" if run.green_fee is not None else "-"
            print(
                f"{run.started_at.astimezone(timezone.utc):%Y-%m-%d %H:%M}Z  "
                f"{run.status.value:<8} "
                f"{request.course.key:<12} {request.target_date}  {slot:<16} {fee:<8} "
                f"{run.detail or ''}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
