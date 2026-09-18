"""Command line entry point: book a tee time, or just list what's available."""

import argparse
import uuid
from dataclasses import replace
from datetime import date, timezone

from loguru import logger
from sqlalchemy import select

from tee_time_booker import dates
from tee_time_booker.config import Settings
from tee_time_booker.courses import CourseConfig
from tee_time_booker.db import BookingPlan, Course, RunStatus, make_session_factory
from tee_time_booker.db.service import (
    claim_request,
    course_config,
    finish_run,
    get_plan,
    plan_preferences,
    recent_runs,
    request_for_plan,
)
from tee_time_booker.models import BookingResult, SearchCriteria
from tee_time_booker.platforms import PaymentOutcomeUnknownError
from tee_time_booker.runner import book_tee_time, find_tee_times


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


def _describe_plan(
    course: CourseConfig, criteria: SearchCriteria, run_id: uuid.UUID
) -> None:
    logger.info(
        f"Run {run_id}: booking {course.name} ({course.platform}) on "
        f"{criteria.target_date:%A %Y-%m-%d} between "
        f"{criteria.earliest:%I:%M %p} and {criteria.latest:%I:%M %p} "
        f"for {criteria.players} players, {criteria.holes} holes"
    )


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
            course = course_config(plan.course)
            prefs = plan_preferences(plan)
            target = settings.target_date_override or dates.target_date(course, prefs)
            if args.find_only:
                criteria = prefs.criteria(target)
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
    _describe_plan(claimed.course, claimed.criteria, claimed.run_id)

    result: BookingResult | None = None
    status = RunStatus.ERROR
    detail: str | None = None
    try:
        result = book_tee_time(
            claimed.course,
            claimed.criteria,
            settings,
            str(claimed.run_id),
            account_env_prefix=claimed.account_env_prefix,
        )
        status = RunStatus.SUCCESS if result.submitted else RunStatus.DRY_RUN
    except PaymentOutcomeUnknownError as e:
        status = RunStatus.UNKNOWN
        detail = str(e)
        logger.exception(f"Unknown payment outcome: {detail}")
    except Exception as e:
        # Error messages can embed whole server responses; keep a short
        # message in the database and the traceback in the job log.
        status = RunStatus.ERROR
        detail = _brief(e)
        logger.exception(f"Error in booking process: {detail}")
    finally:
        with sessions() as session, session.begin():
            finish_run(session, claimed.run_id, status, result=result, detail=detail)

    return 0 if status in (RunStatus.SUCCESS, RunStatus.DRY_RUN) else 1


def _brief(e: Exception, limit: int = 200) -> str:
    """`ExcType: first line of the message`, capped at `limit` characters."""
    message = str(e).splitlines()[0] if str(e) else ""
    text = f"{type(e).__name__}: {message}" if message else type(e).__name__
    return text[:limit]


def _list_courses(sessions) -> int:
    with sessions() as session:
        for course in session.scalars(select(Course).order_by(Course.key)):
            print(f"{course.key:<12} {course.name} ({course.platform})")
    return 0


def _list_plans(sessions) -> int:
    with sessions() as session:
        plans = session.scalars(select(BookingPlan).order_by(BookingPlan.created_at))
        for plan in plans:
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
