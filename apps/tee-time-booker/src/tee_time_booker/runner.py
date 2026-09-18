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

from loguru import logger

from tee_time_booker.browser import browser_session, pause
from tee_time_booker.config import Account, Credentials, PaymentCard, Settings
from tee_time_booker.courses import CourseConfig
from tee_time_booker.dates import check_target_date, release_day, sleep_until_release
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime
from tee_time_booker.platforms import BookingPlatform, build_platform, get_platform
from tee_time_booker.selection import choose_tee_time

# After a release, keep polling for slots to appear this long.
RELEASE_POLL_SECONDS = 15 * 60
RELEASE_POLL_INTERVAL_SECONDS = 3.0
# Log a "still polling" line this often, so a long wait is visibly alive.
POLL_HEARTBEAT_SECONDS = 60
# How many different slots to try holding before giving up.
MAX_HOLD_ATTEMPTS = 4


def load_credentials(
    course: CourseConfig, account_env_prefix: str, *, with_card: bool
) -> Credentials:
    """The login, and the card if the run may pay, from the environment.

    Called before the browser starts so a missing secret fails in a second
    rather than halfway through a booking.
    """
    platform = get_platform(course.platform)
    return Credentials(
        account=Account.from_env(account_env_prefix),
        card=PaymentCard.from_env(platform.required_card_fields) if with_card else None,
    )


@contextmanager
def open_platform(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    *,
    account_env_prefix: str = "",
    with_card: bool = True,
    run_id: str = "",
    credentials: Credentials | None = None,
) -> Iterator[BookingPlatform]:
    """A browser signed in to `course`'s booking site, ready to search.

    Pass `credentials`, or `account_env_prefix` to load them from the
    environment. Anything raised inside leaves a screenshot and the page HTML
    in the artifacts directory; the HTML is what you need to repair a selector.
    """
    if credentials is None:
        credentials = load_credentials(course, account_env_prefix, with_card=with_card)
    with browser_session(
        headless=settings.headless,
        artifacts_dir=settings.artifacts_dir,
        timezone=course.timezone,
        run_id=run_id,
    ) as session:
        platform = build_platform(course, credentials, session)
        try:
            platform.start(criteria)
            check_booking_window(platform, criteria)
            yield platform
        except Exception:
            platform.save_artifacts(f"failure-{course.key}-{criteria.target_date}")
            raise


def check_booking_window(platform: BookingPlatform, criteria: SearchCriteria) -> None:
    """Refuse a target the site can't book yet, once the adapter can ask it.

    Sites without a fixed window (GolfNow) only reveal it on the site itself,
    so this runs signed in, just before the first search. The measured window
    is logged either way: it's the number to plan `weeks_ahead` around.
    """
    course = platform.course
    last = platform.last_bookable_day()
    if last is None:
        return
    today = release_day(course)
    logger.info(
        f"{course.name} takes bookings through {last:%A %Y-%m-%d} "
        f"({(last - today).days} days ahead)"
    )
    check_target_date(course, criteria.target_date, last)


def find_tee_times(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    *,
    account_env_prefix: str,
    poll_seconds: float = 0,
) -> list[TeeTime]:
    """Every slot bookable at `course` for `criteria`.

    A standalone lookup: it opens a browser, signs in, searches and closes
    again. Booking uses `open_platform` directly so the search and the booking
    share one session.
    """
    check_target_date(course, criteria.target_date)
    with open_platform(
        course,
        criteria,
        settings,
        account_env_prefix=account_env_prefix,
        with_card=False,
    ) as platform:
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
    started = time.monotonic()
    deadline = started + poll_seconds
    next_heartbeat = started + POLL_HEARTBEAT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        tee_times = platform.find_tee_times(criteria)
        if tee_times:
            logger.info(f"Found {len(tee_times)} tee times (attempt {attempt})")
            return tee_times
        now = time.monotonic()
        if now >= deadline:
            if poll_seconds:
                raise RuntimeError(
                    f"No tee times on {criteria.target_date} after polling for "
                    f"{poll_seconds / 60:.0f} min ({attempt} searches)"
                )
            return []
        if attempt == 1:
            logger.info(
                f"No tee times yet; polling every {RELEASE_POLL_INTERVAL_SECONDS:g} s "
                f"for up to {poll_seconds / 60:.0f} min"
            )
        elif now >= next_heartbeat:
            logger.info(
                f"Still no tee times after {(now - started) / 60:.0f} min "
                f"({attempt} searches)"
            )
            next_heartbeat = now + POLL_HEARTBEAT_SECONDS
        time.sleep(RELEASE_POLL_INTERVAL_SECONDS)


def book_tee_time(
    course: CourseConfig,
    criteria: SearchCriteria,
    settings: Settings,
    run_id: str,
    *,
    account_env_prefix: str,
    wait_for_release: bool = True,
) -> BookingResult:
    """Book the best available slot at `course`, start to finish.

    Waits for the course's release if one is due, searches, then holds and pays
    for the best slot in the window — falling back to the next-best whenever a
    hold is refused. A run with payment switched off stops before the final
    submit and leaves a screenshot behind.

    `wait_for_release=False` says the target is a day that's already open, so
    there's nothing to wait for even if a release is due tonight.
    """
    platform_type = get_platform(course.platform)
    # Check the date and load secrets before any wait, so a bad one fails now
    # rather than after sleeping up to the release.
    check_target_date(course, criteria.target_date)
    credentials = load_credentials(course, account_env_prefix, with_card=True)
    if wait_for_release:
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
    Only the refused slot itself is skipped: the same time on another side or
    at another rate is a different slot, and the fresh search says whether it's
    still there.
    """
    tried: set[TeeTime] = set()
    for attempt in range(1, MAX_HOLD_ATTEMPTS + 1):
        tee_time = choose_tee_time(tee_times, criteria, exclude=tried)
        logger.info(
            f"Attempt {attempt}: {tee_time.start:%Y-%m-%d %I:%M %p} "
            f"{tee_time.side or ''} ${tee_time.green_fee}"
        )
        if platform.hold(tee_time, criteria):
            return tee_time
        tried.add(tee_time)
        pause(1.5, 3.0)
        tee_times = platform.find_tee_times(criteria)
    raise RuntimeError(
        f"Could not hold any slot on {criteria.target_date} in "
        f"{MAX_HOLD_ATTEMPTS} attempts"
    )
