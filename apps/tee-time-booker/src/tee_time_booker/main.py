"""Command line entry point: book a tee time, or just list what's available."""

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone

from loguru import logger

from tee_time_booker import dates
from tee_time_booker.config import Settings
from tee_time_booker.courses import COURSES, CourseConfig, get_course
from tee_time_booker.models import BookingResult, SearchCriteria
from tee_time_booker.platforms import PaymentOutcomeUnknownError
from tee_time_booker.run_log import RunRecord, log_run
from tee_time_booker.runner import book_tee_time, find_tee_times


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tee-time-booker",
        description="Book a tee time on whichever site the course uses.",
    )
    parser.add_argument(
        "--course",
        help="Course to book (default: TEE_TIME_COURSE, else soule-park)",
    )
    parser.add_argument(
        "--date",
        help="Book this YYYY-MM-DD instead of the course's computed target date",
    )
    parser.add_argument(
        "--find-only",
        action="store_true",
        help="List the available tee times and exit without booking",
    )
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="Show the configured courses and exit",
    )
    return parser.parse_args(argv)


def _criteria(course: CourseConfig, settings: Settings) -> SearchCriteria:
    target = settings.target_date_override or dates.target_date(course)
    return course.criteria(target)


def _describe_plan(course: CourseConfig, criteria: SearchCriteria, run_id: str) -> None:
    logger.info(
        f"Run {run_id}: booking {course.name} ({course.platform}) on "
        f"{criteria.target_date:%A %Y-%m-%d} between "
        f"{criteria.earliest:%I:%M %p} and {criteria.latest:%I:%M %p} "
        f"for {criteria.players} players, {criteria.holes} holes"
    )


def main(argv: list[str] | None = None) -> int:
    """Book one tee time, append the outcome to the CSV log, return an exit code."""
    args = parse_args(argv)
    if args.list_courses:
        for key, course in sorted(COURSES.items()):
            print(f"{key:<12} {course.name} ({course.platform})")
        return 0

    # Command line arguments win over the environment for the same settings.
    settings = Settings.from_env()
    if args.course:
        settings = replace(settings, course_key=args.course)
    if args.date:
        settings = replace(settings, target_date_override=date.fromisoformat(args.date))
    course = get_course(settings.course_key)
    criteria = _criteria(course, settings)

    if args.find_only:
        for tee_time in find_tee_times(course, criteria, settings):
            fee = "" if tee_time.green_fee is None else f" ${tee_time.green_fee:.2f}"
            side = f" {tee_time.side}" if tee_time.side else ""
            print(f"{tee_time.start:%Y-%m-%d %I:%M %p}{side}{fee}")
        return 0

    created_at = datetime.now(timezone.utc)
    run_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    _describe_plan(course, criteria, run_id)

    result: BookingResult | None = None
    status = "Failed"
    try:
        result = book_tee_time(course, criteria, settings, run_id)
        status = result.status
    except PaymentOutcomeUnknownError as e:
        status = f"Unknown: {e}"
        logger.exception(status)
    except Exception as e:
        # log.csv is committed to the repo, so keep only a short message there
        # (error messages can embed server responses); the traceback goes to
        # the job log.
        status = f"Error in booking process: {_brief(e)}"
        logger.exception(status)
    finally:
        tee_time = result.tee_time if result else None
        log_run(
            RunRecord(
                run_id=run_id,
                created_at=created_at,
                finished_at=datetime.now(timezone.utc),
                course_name=course.name if tee_time else None,
                tee_time_pst=tee_time.start if tee_time else None,
                cost_dollars=tee_time.green_fee if tee_time else None,
                service_fee_dollars=tee_time.service_fee if tee_time else None,
                status=status,
            ),
            settings.log_path,
        )

    return 0 if status.startswith("Success") else 1


def _brief(e: Exception, limit: int = 200) -> str:
    """`ExcType: first line of the message`, capped at `limit` characters."""
    message = str(e).splitlines()[0] if str(e) else ""
    text = f"{type(e).__name__}: {message}" if message else type(e).__name__
    return text[:limit]


if __name__ == "__main__":
    raise SystemExit(main())
