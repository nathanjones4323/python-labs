"""The scheduler over a real database, with the browser faked out.

Times are Pacific, the seeded courses' timezone: Soule Park opens each day
at midnight 20 days ahead; Saticoy (GolfNow) has a rolling window that's
only known once signed in.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from tee_time_booker.config import Settings
from tee_time_booker.courses import SATICOY, SOULE_PARK
from tee_time_booker.dates import NotYetBookable, check_target_date
from tee_time_booker.db import BookingPlan, BookingRequest, RunStatus
from tee_time_booker.db.service import (
    claim_request,
    create_request,
    finish_run,
    get_course,
    get_user,
    site_account_for,
)
from tee_time_booker.models import BookingResult, SearchCriteria, TeeTime
from tee_time_booker.scheduler import first_attempt, start_due
from tee_time_booker.worker import BookingWorker

PT = ZoneInfo("America/Los_Angeles")
SLOT = TeeTime(
    platform="golfnow", course_name="Saticoy", start=datetime(2026, 11, 7, 8)
)


def at(*args) -> datetime:
    return datetime(*args, tzinfo=PT)


@pytest.fixture
def worker(db, tmp_path):
    settings = Settings(
        database_url=None,
        plan_id=None,
        submit_payment=True,
        headless=True,
        artifacts_dir=tmp_path,
        target_date_override=None,
    )
    pool = BookingWorker(db, settings, max_workers=2)
    yield pool
    pool.shutdown()


def tick(db, worker, now):
    """One scheduler tick, waiting for whatever it started to finish."""
    started = start_due(db, worker, now)
    for item in started:
        future = worker.futures.get(item.claimed.run_id)
        if future is not None:
            future.result(timeout=10)
    return started


def arm_plan(db, plan_id, on=True):
    with db() as session, session.begin():
        session.get(BookingPlan, plan_id).auto_book = on


def one_off(db, course_key, target, *, auto_book=True):
    with db() as session, session.begin():
        user = get_user(session, "golfer@example.com")
        course = get_course(session, course_key)
        request = create_request(
            session,
            user,
            course,
            site_account_for(session, user, course),
            SearchCriteria(
                target_date=target,
                earliest=time(7),
                latest=time(10),
                players=2,
                holes=18,
            ),
            auto_book=auto_book,
        )
        return request.id


def request_state(db, request_id):
    with db() as session:
        request = session.get(BookingRequest, request_id)
        return request.status.value, [(r.status, r.scheduled) for r in request.runs]


# --- when -------------------------------------------------------------------


def test_a_release_course_is_first_tried_just_before_the_release():
    # Nov 14 opens at midnight 20 days before: Sun Oct 25.
    assert first_attempt(SOULE_PARK, date(2026, 11, 14)) == at(2026, 10, 24, 23, 50)


def test_a_rolling_course_is_first_tried_a_morning_thirty_days_out():
    assert first_attempt(SATICOY, date(2026, 11, 14)) == at(2026, 10, 15, 6, 0)


def test_a_day_past_the_window_is_not_bookable_yet():
    with pytest.raises(NotYetBookable):
        check_target_date(SOULE_PARK, date(2026, 12, 1), now=at(2026, 10, 21, 12))


# --- plans -------------------------------------------------------------------


def test_an_auto_booked_plan_is_booked_at_its_release(db, seeded, worker, fake_booking):
    seen = {}
    fake_booking(BookingResult(tee_time=SLOT, submitted=True), seen)
    arm_plan(db, seeded["soule_plan"])

    # Saturday evening: the plan's next date is Nov 14, which opens at midnight.
    assert tick(db, worker, at(2026, 10, 24, 23, 30)) == []
    [started] = tick(db, worker, at(2026, 10, 24, 23, 51))

    assert started.claimed.criteria.target_date == date(2026, 11, 14)
    assert started.dry_run is False
    assert seen["wait"] is True and seen["settings"].submit_payment is True
    status, runs = request_state(db, started.claimed.request_id)
    assert status == "booked" and runs == [(RunStatus.SUCCESS, True)]
    # Booked is done: the next tick leaves it alone.
    assert tick(db, worker, at(2026, 10, 24, 23, 55)) == []


def test_a_day_already_open_is_booked_at_once_without_waiting(
    db, seeded, worker, fake_booking
):
    seen = {}
    fake_booking(BookingResult(tee_time=SLOT, submitted=True), seen)
    arm_plan(db, seeded["soule_plan"])
    # Wednesday: the 3rd Saturday out (Nov 7) opened on Oct 18.
    [started] = tick(db, worker, at(2026, 10, 21, 12))
    assert started.claimed.criteria.target_date == date(2026, 11, 7)
    assert seen["wait"] is False


def test_plans_that_arent_auto_booked_are_left_alone(db, seeded, worker, fake_booking):
    fake_booking(BookingResult(tee_time=SLOT, submitted=True))
    assert tick(db, worker, at(2026, 10, 24, 23, 51)) == []
    # Pausing an armed plan stops its waiting request too.
    arm_plan(db, seeded["soule_plan"])
    assert tick(db, worker, at(2026, 10, 24, 23, 0)) == []  # creates the request
    arm_plan(db, seeded["soule_plan"], on=False)
    assert tick(db, worker, at(2026, 10, 24, 23, 51)) == []


def test_a_failed_scheduled_booking_is_not_retried(db, seeded, worker, fake_booking):
    fake_booking(RuntimeError("No slot in the window"))
    arm_plan(db, seeded["soule_plan"])
    [started] = tick(db, worker, at(2026, 10, 24, 23, 51))
    assert request_state(db, started.claimed.request_id)[0] == "failed"
    assert tick(db, worker, at(2026, 10, 25, 9, 0)) == []


# --- one-offs ----------------------------------------------------------------


def test_a_rolling_one_off_is_tried_each_morning_until_it_opens(
    db, seeded, worker, fake_booking
):
    request_id = one_off(db, "saticoy", date(2026, 11, 7))
    fake_booking(NotYetBookable("beyond its booking window"))

    # Too early in the day, then the morning try finds it shut.
    assert tick(db, worker, at(2026, 10, 21, 5, 0)) == []
    assert len(tick(db, worker, at(2026, 10, 21, 7, 0))) == 1
    assert request_state(db, request_id) == ("pending", [(RunStatus.NOT_OPEN, True)])
    # Once a day only.
    assert tick(db, worker, at(2026, 10, 21, 15, 0)) == []

    fake_booking(BookingResult(tee_time=SLOT, submitted=True))
    assert len(tick(db, worker, at(2026, 10, 22, 6, 1))) == 1
    status, runs = request_state(db, request_id)
    assert status == "booked"
    assert runs == [(RunStatus.NOT_OPEN, True), (RunStatus.SUCCESS, True)]


def test_one_offs_too_far_out_past_or_unarmed_are_not_tried(
    db, seeded, worker, fake_booking
):
    fake_booking(BookingResult(tee_time=SLOT, submitted=True))
    one_off(db, "saticoy", date(2026, 12, 25))  # more than 30 days out
    one_off(db, "saticoy", date(2026, 10, 20))  # yesterday
    one_off(db, "saticoy", date(2026, 11, 7), auto_book=False)
    assert tick(db, worker, at(2026, 10, 21, 7, 0)) == []


def test_a_manual_dry_run_doesnt_use_up_the_scheduled_attempt(
    db, seeded, worker, fake_booking
):
    request_id = one_off(db, "saticoy", date(2026, 11, 7))
    with db() as session, session.begin():
        claimed = claim_request(session, request_id)
    with db() as session, session.begin():
        finish_run(session, claimed.run_id, RunStatus.DRY_RUN)

    fake_booking(BookingResult(tee_time=SLOT, submitted=True))
    assert len(tick(db, worker, at(2026, 10, 21, 7, 0))) == 1
    assert request_state(db, request_id)[0] == "booked"
