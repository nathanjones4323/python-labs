"""The CLI's run bookkeeping: what lands in the database for each outcome."""

from datetime import date, datetime

import pytest
from sqlalchemy import select
from tee_time_booker import main as cli
from tee_time_booker.db import BookingRequest, RequestStatus, Run, RunStatus
from tee_time_booker.models import BookingResult, TeeTime
from tee_time_booker.platforms import PaymentOutcomeUnknownError


@pytest.fixture
def env(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("TEE_TIME_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("TEE_TIME_PLAN", str(seeded["soule_plan"]))
    monkeypatch.delenv("TEE_TIME_TARGET_DATE", raising=False)
    return seeded


def _only_run(db):
    with db() as session:
        (run,) = session.scalars(select(Run)).all()
        request = session.get(BookingRequest, run.request_id)
        return run, request


def _fake_booking(outcome, seen=None):
    def book_tee_time(course, criteria, settings, run_id, *, account_env_prefix):
        if seen is not None:
            seen.update(
                course=course,
                criteria=criteria,
                run_id=run_id,
                prefix=account_env_prefix,
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return book_tee_time


def test_successful_dry_run_records_the_slot_and_leaves_the_request_pending(
    db, env, monkeypatch
):
    slot = TeeTime(
        platform="foreup",
        course_name="Soule Park",
        start=datetime(2026, 10, 3, 8, 0),
        green_fee=56.0,
        side="Front",
    )
    seen = {}
    monkeypatch.setattr(
        cli,
        "book_tee_time",
        _fake_booking(BookingResult(tee_time=slot, submitted=False), seen),
    )

    assert cli.main(["--date", "2026-10-03"]) == 0
    run, request = _only_run(db)
    assert run.status is RunStatus.DRY_RUN
    assert run.tee_time_local == datetime(2026, 10, 3, 8, 0)
    assert run.side == "Front" and float(run.green_fee) == 56.0
    assert request.status is RequestStatus.PENDING
    # The runner was handed what the plan says, not anything from the env.
    assert seen["course"].key == "soule-park"
    assert seen["prefix"] == "SOULE"
    assert seen["run_id"] == str(run.id)


def test_a_paid_booking_marks_the_request_booked(db, env, monkeypatch):
    slot = TeeTime(
        platform="foreup", course_name="Soule Park", start=datetime(2026, 10, 3, 8, 0)
    )
    monkeypatch.setattr(
        cli,
        "book_tee_time",
        _fake_booking(BookingResult(tee_time=slot, submitted=True, confirmation="R1")),
    )
    assert cli.main(["--date", "2026-10-03"]) == 0
    run, request = _only_run(db)
    assert run.status is RunStatus.SUCCESS and run.confirmation == "R1"
    assert request.status is RequestStatus.BOOKED


def test_unknown_payment_outcome_is_neither_success_nor_error(db, env, monkeypatch):
    monkeypatch.setattr(
        cli,
        "book_tee_time",
        _fake_booking(PaymentOutcomeUnknownError("no confirmation within 30 s")),
    )
    assert cli.main(["--date", "2026-10-03"]) == 1
    run, request = _only_run(db)
    assert run.status is RunStatus.UNKNOWN
    assert run.detail == "no confirmation within 30 s"
    assert request.status is RequestStatus.UNKNOWN


def test_errors_record_a_short_message_not_a_traceback(db, env, monkeypatch):
    error = RuntimeError("Login failed (403): <html>" + "x" * 500 + "\nmore")
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(error))

    assert cli.main(["--date", "2026-10-03"]) == 1
    run, request = _only_run(db)
    assert run.status is RunStatus.ERROR
    assert run.detail.startswith("RuntimeError: Login")
    assert "\n" not in run.detail and "Traceback" not in run.detail
    assert len(run.detail) <= 200
    assert request.status is RequestStatus.FAILED


def test_a_failed_request_is_retried_by_the_next_run_for_that_date(
    db, env, monkeypatch
):
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(RuntimeError("stop")))
    cli.main(["--date", "2026-10-03"])
    cli.main(["--date", "2026-10-03"])
    with db() as session:
        (request,) = session.scalars(select(BookingRequest)).all()
        assert len(request.runs) == 2


def test_brief_handles_empty_messages():
    assert cli._brief(ValueError()) == "ValueError"
    assert cli._brief(ValueError("bad\nworse")) == "ValueError: bad"


def test_date_flag_overrides_the_computed_target(db, env, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(RuntimeError("stop"), seen))
    cli.main(["--date", "2026-10-03"])
    assert seen["criteria"].target_date == date(2026, 10, 3)


def test_request_flag_runs_that_request(db, env, monkeypatch):
    from tee_time_booker.db.service import get_plan, request_for_plan

    with db() as session, session.begin():
        request_id = request_for_plan(
            session, get_plan(session, env["saticoy_plan"]), date(2026, 9, 26)
        ).id
    seen = {}
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(RuntimeError("stop"), seen))
    cli.main(["--request", str(request_id)])
    assert seen["course"].key == "saticoy" and seen["prefix"] == "GOLFNOW"
    assert seen["criteria"].target_date == date(2026, 9, 26)


def test_without_a_plan_or_request_the_cli_says_so(db, env, monkeypatch):
    monkeypatch.delenv("TEE_TIME_PLAN")
    with pytest.raises(SystemExit, match="--plan"):
        cli.main([])


def test_listings_run_without_booking(db, env, capsys):
    assert cli.main(["--list-courses"]) == 0
    assert cli.main(["--list-plans"]) == 0
    assert cli.main(["--runs", "5"]) == 0
    out = capsys.readouterr().out
    assert "soule-park" in out and "saticoy-saturday" in out
