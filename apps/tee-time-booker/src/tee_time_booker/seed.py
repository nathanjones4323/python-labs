"""Load the known courses, and optionally one user's accounts and plans.

    python -m tee_time_booker.seed                      # courses only
    python -m tee_time_booker.seed --user you@example.com

Idempotent: rerunning updates the rows it created rather than duplicating
them, so this is also how a change to `courses.SEED_COURSES` reaches the
database. The user's plans reproduce the preferences the app booked with
before it had a database, one per course, and their ids are printed so one
can go in `TEE_TIME_PLAN`.
"""

import argparse
import os
from datetime import time

from tee_time_booker.config import Settings
from tee_time_booker.courses import SATICOY, SEED_COURSES, SOULE_PARK, CourseConfig
from tee_time_booker.db import make_session_factory
from tee_time_booker.db.service import (
    ensure_plan,
    ensure_site_account,
    ensure_user,
    upsert_course,
)
from tee_time_booker.models import Preferences

SATURDAY = 5

# The env-var prefix each seeded course's login is read from (`<PREFIX>_EMAIL`,
# `<PREFIX>_PASSWORD`), i.e. the variables the GitHub workflow already sets.
ACCOUNT_ENV_PREFIXES: dict[str, str] = {
    SOULE_PARK.key: "SOULE",
    SATICOY.key: "GOLFNOW",
}

DEFAULT_PLANS: dict[str, Preferences] = {
    SOULE_PARK.key: Preferences(
        target_weekday=SATURDAY,
        weeks_ahead=3,  # the 3rd upcoming Saturday: exactly foreUP's 20-day window
        earliest=time(8, 0),
        latest=time(14, 0),
        holes=18,
        players=4,
    ),
    SATICOY.key: Preferences(
        target_weekday=SATURDAY,
        weeks_ahead=1,  # GolfNow lists roughly a week out for Saticoy
        earliest=time(7, 0),
        latest=time(11, 0),
        holes=9,  # Saticoy sells 9-hole rates on GolfNow
        players=4,
    ),
}


def seed_user(
    session,
    email: str,
    name: str | None,
    courses: dict[str, CourseConfig],
    keys: set[str] | None = None,
):
    """The user, a site account and the default plan for each course in `keys`
    (default: every course with a default plan)."""
    user = ensure_user(session, email, name)
    plans = []
    for key, prefs in DEFAULT_PLANS.items():
        if keys is not None and key not in keys:
            continue
        course = upsert_course(session, courses[key])
        prefix = ACCOUNT_ENV_PREFIXES[key]
        account = ensure_site_account(
            session,
            user,
            platform=course.platform,
            login_email=os.environ.get(f"{prefix}_EMAIL", "").strip() or email,
            env_prefix=prefix,
        )
        plans.append(
            ensure_plan(
                session,
                user,
                name=f"{key}-saturday",
                course=course,
                site_account=account,
                preferences=prefs,
            )
        )
    return plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--user", help="Email of a user to create with the default plans"
    )
    parser.add_argument("--name", help="Display name for --user")
    args = parser.parse_args(argv)

    sessions = make_session_factory(Settings.from_env().require_database_url())
    courses = {c.key: c for c in SEED_COURSES}
    with sessions() as session, session.begin():
        for config in SEED_COURSES:
            upsert_course(session, config)
        print(f"Courses: {', '.join(courses)}")
        if args.user:
            plans = seed_user(session, args.user, args.name, courses)
            session.flush()
            for plan in plans:
                print(f"Plan {plan.name}: {plan.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
