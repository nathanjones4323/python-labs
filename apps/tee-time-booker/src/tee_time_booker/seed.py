"""Load the known courses, and optionally one user and their site accounts.

    python -m tee_time_booker.seed                      # courses only
    python -m tee_time_booker.seed --user you@example.com

Idempotent: rerunning updates the rows it created rather than duplicating
them, so this is also how a change to `courses.SEED_COURSES` reaches the
database. No plans are made: a plan is a weekly booking, so it's only created
when the person asks for one (through the bot, the API or MCP).
"""

import argparse
import os

from tee_time_booker.config import Settings
from tee_time_booker.courses import SATICOY, SEED_COURSES, SOULE_PARK, CourseConfig
from tee_time_booker.db import make_session_factory
from tee_time_booker.db.service import (
    ensure_site_account,
    ensure_user,
    upsert_course,
)
from tee_time_booker.platforms import get_platform

# The env-var prefix each login is read from (`<PREFIX>_EMAIL`,
# `<PREFIX>_PASSWORD`), i.e. the variables in `.env.example`. A site that signs
# in to each course separately (foreUP) has a login per course, keyed by the
# course; one that signs in site-wide (GolfNow) has one login for all of its
# courses, keyed by the site.
ACCOUNT_ENV_PREFIXES: dict[str, str] = {SOULE_PARK.key: "SOULE", "golfnow": "GOLFNOW"}


def account_env_prefix(course: CourseConfig) -> str:
    """The env-var prefix of the login that books `course`; one added since is
    its course key or site name in capitals."""
    if get_platform(course.platform).signs_in_per_course:
        key = course.key
    else:
        key = course.platform
    return ACCOUNT_ENV_PREFIXES.get(key, key.upper().replace("-", "_"))


# The courses whose logins a seeded user gets when none are chosen.
DEFAULT_LOGIN_COURSES = (SOULE_PARK, SATICOY)


def seed_user(
    session,
    email: str,
    name: str | None,
    courses: dict[str, CourseConfig],
    prefixes: set[str] | None = None,
):
    """The user and an account for each login in `prefixes` (default: the
    logins `DEFAULT_LOGIN_COURSES` book with)."""
    if prefixes is None:
        prefixes = {account_env_prefix(c) for c in DEFAULT_LOGIN_COURSES}
    user = ensure_user(session, email, name)
    accounts = {}
    for config in courses.values():
        prefix = account_env_prefix(config)
        if prefix not in prefixes or prefix in accounts:
            continue
        accounts[prefix] = ensure_site_account(
            session,
            user,
            platform=config.platform,
            login_email=os.environ.get(f"{prefix}_EMAIL", "").strip() or email,
            env_prefix=prefix,
        )
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--user", help="Email of a user to create, with their site accounts"
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
            seed_user(session, args.user, args.name, courses)
            print(f"User: {args.user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
