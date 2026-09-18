"""The request state machine and the service layer, against a real Postgres."""

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tee_time_booker.courses import SATICOY, SOULE_PARK, GolfNowCourse
from tee_time_booker.db import (
    BookingRequest,
    Course,
    InvalidTransition,
    RequestStatus,
    Run,
    RunStatus,
)
from tee_time_booker.db.models import REQUEST_TRANSITIONS
from tee_time_booker.db.service import (
    claim_request,
    course_config,
    finish_run,
    get_plan,
    plan_preferences,
    recent_runs,
    request_for_plan,
    upsert_course,
)
from tee_time_booker.models import BookingResult, TeeTime


def _request(session, plan_id, target=date(2026, 10, 3)):
    return request_for_plan(session, get_plan(session, plan_id), target)


# --- state machine (no database needed) -------------------------------------


def test_every_status_has_a_transition_entry():
    assert set(REQUEST_TRANSITIONS) == set(RequestStatus)


@pytest.mark.parametrize(
    "start, to",
    [
        (RequestStatus.PENDING, RequestStatus.BOOKED),  # must run first
        (RequestStatus.BOOKED, RequestStatus.RUNNING),  # never book twice
        (RequestStatus.CANCELLED, RequestStatus.PENDING),  # no resurrection
        (RequestStatus.UNKNOWN, RequestStatus.RUNNING),  # a person resolves it
    ],
)
def test_forbidden_transitions_raise(start, to):
    request = BookingRequest(status=start)
    with pytest.raises(InvalidTransition):
        request.transition(to)
    assert request.status is start


def test_failed_requests_can_be_retried_or_cancelled():
    request = BookingRequest(status=RequestStatus.FAILED)
    request.transition(RequestStatus.RUNNING)
    request.transition(RequestStatus.FAILED)
    request.transition(RequestStatus.CANCELLED)


# --- courses ---------------------------------------------------------------


def test_courses_round_trip_through_the_table(db):
    with db() as session, session.begin():
        upsert_course(session, SATICOY)
    with db() as session:
        row = session.scalar(select(Course).where(Course.key == "saticoy"))
        config = course_config(row)
    assert isinstance(config, GolfNowCourse)
    assert config == SATICOY


def test_upsert_updates_rather_than_duplicates(db):
    with db() as session, session.begin():
        upsert_course(session, SOULE_PARK)
        upsert_course(session, SOULE_PARK)
    with db() as session:
        assert session.scalars(select(Course)).all().__len__() == 1


# --- plans and requests -----------------------------------------------------


def test_plan_preferences_come_back_as_seeded(db, seeded):
    with db() as session:
        prefs = plan_preferences(get_plan(session, seeded["soule_plan"]))
    assert (prefs.target_weekday, prefs.weeks_ahead) == (5, 3)
    assert (prefs.holes, prefs.players) == (18, 4)


def test_a_plan_gets_one_request_per_date(db, seeded):
    with db() as session, session.begin():
        first = _request(session, seeded["soule_plan"]).id
        again = _request(session, seeded["soule_plan"]).id
        other = _request(session, seeded["soule_plan"], date(2026, 10, 10)).id
    assert first == again != other


def test_request_copies_the_plans_search(db, seeded):
    with db() as session, session.begin():
        request = _request(session, seeded["saticoy_plan"])
        assert request.status is RequestStatus.PENDING
        assert (request.holes, request.players) == (9, 4)
        assert request.course.key == "saticoy"
        assert request.site_account.env_prefix == "GOLFNOW"


def test_two_requests_for_a_plan_and_date_are_refused_by_the_database(db, seeded):
    with db() as session, session.begin():
        plan = get_plan(session, seeded["soule_plan"])
        for _ in range(2):
            session.add(
                BookingRequest(
                    user_id=plan.user_id,
                    course_id=plan.course_id,
                    site_account_id=plan.site_account_id,
                    plan_id=plan.id,
                    target_date=date(2026, 10, 3),
                    earliest=plan.earliest,
                    latest=plan.latest,
                    holes=plan.holes,
                    players=plan.players,
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()


# --- claiming and finishing runs ------------------------------------------


def test_claim_opens_a_run_and_marks_the_request_running(db, seeded):
    with db() as session, session.begin():
        request_id = _request(session, seeded["soule_plan"]).id
    with db() as session, session.begin():
        claimed = claim_request(session, request_id)
    assert claimed.course == SOULE_PARK
    assert claimed.account_env_prefix == "SOULE"
    assert claimed.criteria.target_date == date(2026, 10, 3)
    with db() as session:
        request = session.get(BookingRequest, request_id)
        assert request.status is RequestStatus.RUNNING
        (run,) = request.runs
        assert run.id == claimed.run_id and run.status is RunStatus.RUNNING


def test_a_running_request_cannot_be_claimed_again(db, seeded):
    with db() as session, session.begin():
        request_id = _request(session, seeded["soule_plan"]).id
        claim_request(session, request_id)
    with db() as session, session.begin():
        with pytest.raises(InvalidTransition):
            claim_request(session, request_id)


@pytest.mark.parametrize(
    "run_status, request_status",
    [
        (RunStatus.SUCCESS, RequestStatus.BOOKED),
        (RunStatus.DRY_RUN, RequestStatus.PENDING),
        (RunStatus.ERROR, RequestStatus.FAILED),
        (RunStatus.UNKNOWN, RequestStatus.UNKNOWN),
    ],
)
def test_finishing_a_run_moves_the_request_on(db, seeded, run_status, request_status):
    with db() as session, session.begin():
        claimed = claim_request(session, _request(session, seeded["soule_plan"]).id)
    with db() as session, session.begin():
        finish_run(session, claimed.run_id, run_status, detail="x")
    with db() as session:
        run = session.get(Run, claimed.run_id)
        assert run.status is run_status and run.finished_at is not None
        assert run.request.status is request_status


def test_finishing_with_a_result_records_the_slot(db, seeded):
    slot = TeeTime(
        platform="golfnow",
        course_name="Saticoy Regional Golf Course",
        start=datetime(2026, 9, 19, 7, 24),
        green_fee=31.0,
        service_fee=3.99,
        side=None,
    )
    with db() as session, session.begin():
        claimed = claim_request(session, _request(session, seeded["saticoy_plan"]).id)
    with db() as session, session.begin():
        finish_run(
            session,
            claimed.run_id,
            RunStatus.SUCCESS,
            result=BookingResult(tee_time=slot, submitted=True, confirmation="ABC123"),
        )
    with db() as session:
        (run,) = recent_runs(session)
        assert run.tee_time_local == datetime(2026, 9, 19, 7, 24)
        assert float(run.green_fee) == 31.0 and float(run.service_fee) == 3.99
        assert run.confirmation == "ABC123"
