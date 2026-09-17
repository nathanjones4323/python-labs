"""The booking operations, one code path for every platform.

Adapters supply the mechanics of a site; this module supplies the policy that
should be identical whichever site is being booked: wait for the release, poll
until the day's sheet is open, pick the best slot, retry the next-best when a
hold is refused.

These two functions are the app's real interface — the CLI calls them today and
an HTTP layer should call them tomorrow, rather than reaching for an adapter.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from loguru import logger

from tee_time_booker.browser import browser_session, pause
from tee_time_booker.config import Account, Credentials, PaymentCard, Settings
from tee_time_booker.courses import CourseConfig
from tee_time_booker.dates import sleep_until_release
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime
from tee_time_booker.platforms import BookingPlatform, build_platform, get_platform
from tee_time_booker.selection import choose_tee_time

# After a release, keep polling for slots to appear this long.
RELEASE_POLL_SECONDS = 15 * 60
RELEASE_POLL_INTERVAL_SECONDS = 3.0
# How many different slots to try holding before giving up.
MAX_HOLD_ATTEMPTS = 4


def load_credentials(course: CourseConfig, *, with_card: bool) -> Credentials:
    """The login, and the card if the run may pay, from the environment.

    Called before the browser starts so a missing secret fails in a second
    rather than halfway through a booking.
    """
    platform = get_platform(course.platform)
    return Credentials(
        account=Account.from_env(course),
        card=PaymentCard.from_env(platform.required_card_fields) if with_card else None,
    )


@contextmanager
def open_platform(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    *,
    with_card: bool = True,
    run_id: str = "",
    credentials: Credentials | None = None,
) -> Iterator[BookingPlatform]:
    """A browser signed in to `course`'s booking site, ready to search.

    Anything raised inside leaves a screenshot and the page HTML in the
    artifacts directory; the HTML is what you need to repair a selector.
    """
    if credentials is None:
        credentials = load_credentials(course, with_card=with_card)
    with browser_session(
        headless=settings.headless,
        artifacts_dir=settings.artifacts_dir,
        timezone=course.timezone,
        run_id=run_id,
    ) as session:
        platform = build_platform(course, credentials, session)
        try:
            platform.start(criteria)
            yield platform
        except Exception:
            session.save_artifacts(f"failure-{course.key}-{criteria.target_date}")
            raise


def find_tee_times(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    *,
    poll_seconds: float = 0,
) -> list[TeeTime]:
    """Every slot bookable at `course` for `criteria`.

    A standalone lookup: it opens a browser, signs in, searches and closes
    again. Booking uses `open_platform` directly so the search and the booking
    share one session.
    """
    with open_platform(course, criteria, settings, with_card=False) as platform:
        return search(platform, criteria, poll_seconds=poll_seconds)


def search(
    platform: BookingPlatform, criteria: SearchCriteria, *, poll_seconds: float = 0
) -> list[TeeTime]:
    """Search, optionally polling while the day still isn't open for booking.

    Sites return nothing for a day outside their booking window, so an empty
    result just after a release most likely means the sheet is a few seconds
    late rather than that the day is sold out.
    """
    logger.info(f"Fetching tee times for {criteria.target_date:%A %Y-%m-%d}")
    deadline = time.monotonic() + poll_seconds
    attempt = 0
    while True:
        attempt += 1
        tee_times = platform.find_tee_times(criteria)
        if tee_times:
            logger.info(f"Found {len(tee_times)} tee times (attempt {attempt})")
            return tee_times
        if time.monotonic() >= deadline:
            if poll_seconds:
                raise RuntimeError(
                    f"No tee times on {criteria.target_date} after polling for "
                    f"{poll_seconds / 60:.0f} min"
                )
            return []
        if attempt == 1:
            logger.info("No tee times yet; polling until they appear")
        time.sleep(RELEASE_POLL_INTERVAL_SECONDS)


def book_tee_time(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    run_id: str = "",
) -> BookingResult:
    """Book the best available slot at `course`, start to finish.

    Waits for the course's release if one is due, searches, then holds and pays
    for the best slot in the window — falling back to the next-best whenever a
    hold is refused. A run with payment switched off stops before the final
    submit and leaves a screenshot behind.
    """
    platform_type = get_platform(course.platform)
    # Load secrets before any wait, so a missing one fails now rather than
    # after sleeping up to the release.
    credentials = load_credentials(course, with_card=True)
    if settings.target_date_override is None:
        # An override points at an already-open day; there's nothing to wait for.
        sleep_until_release(course, platform_type.release_lead_seconds)

    with open_platform(
        course, criteria, settings, run_id=run_id, credentials=credentials
    ) as platform:
        tee_times = search(platform, criteria, poll_seconds=RELEASE_POLL_SECONDS)
        tee_time = hold_best(platform, criteria, tee_times)
        result = platform.complete_booking(
            tee_time, criteria, submit_payment=settings.submit_payment
        )
        logger.success(f"{result.status}: {tee_time.start:%A %Y-%m-%d %I:%M %p}")
        return result


def hold_best(
    platform: BookingPlatform, criteria: SearchCriteria, tee_times: list[TeeTime]
) -> TeeTime:
    """Hold the best slot in the window, moving on when one is refused.

    A hold is refused when somebody else took the slot between the search and
    the click, so each retry searches again rather than trusting the old list.
    Refused start times are skipped wholesale: if a site offers the same time
    on two sides or at two rates, that time is gone for all of them.
    """
    tried: set[datetime] = set()
    for attempt in range(1, MAX_HOLD_ATTEMPTS + 1):
        tee_time = choose_tee_time(tee_times, criteria, exclude=tried)
        logger.info(
            f"Attempt {attempt}: {tee_time.start:%Y-%m-%d %I:%M %p} "
            f"{tee_time.side or ''} ${tee_time.green_fee}"
        )
        if platform.hold(tee_time, criteria):
            return tee_time
        tried.add(tee_time.start)
        pause(1.5, 3.0)
        tee_times = platform.find_tee_times(criteria)
    raise RuntimeError(
        f"Could not hold any slot on {criteria.target_date} in "
        f"{MAX_HOLD_ATTEMPTS} attempts"
    )
