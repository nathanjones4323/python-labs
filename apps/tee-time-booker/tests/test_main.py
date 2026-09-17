"""The CLI's run log: what lands in the committed CSV for each outcome."""

import csv
from datetime import date, datetime

import pytest
from tee_time_booker import main as cli
from tee_time_booker.models import BookingResult, TeeTime
from tee_time_booker.platforms import PaymentOutcomeUnknownError


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "log.csv"
    monkeypatch.setenv("TEE_TIME_LOG_PATH", str(path))
    monkeypatch.setenv("TEE_TIME_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("TEE_TIME_TARGET_DATE", raising=False)
    monkeypatch.delenv("TEE_TIME_COURSE", raising=False)
    return path


def _rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def _fake_booking(outcome):
    def book_tee_time(course, criteria, settings, run_id):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return book_tee_time


def test_successful_dry_run_logs_the_slot(log_path, monkeypatch):
    slot = TeeTime(
        platform="foreup",
        course_name="Soule Park",
        start=datetime(2026, 10, 3, 8, 0),
        green_fee=56.0,
    )
    result = BookingResult(tee_time=slot, submitted=False)
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(result))

    assert cli.main(["--date", "2026-10-03"]) == 0
    (row,) = _rows(log_path)
    assert row["status"] == "Success (dry run)"
    assert row["course_name"] == "Soule Park"
    assert row["cost_dollars"] == "56.0"


def test_unknown_payment_outcome_is_neither_success_nor_error(log_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "book_tee_time",
        _fake_booking(PaymentOutcomeUnknownError("no confirmation within 30 s")),
    )
    assert cli.main(["--date", "2026-10-03"]) == 1
    (row,) = _rows(log_path)
    assert row["status"] == "Unknown: no confirmation within 30 s"


def test_errors_log_a_short_message_not_a_traceback(log_path, monkeypatch):
    error = RuntimeError("Login failed (403): <html>" + "x" * 500 + "\nmore")
    monkeypatch.setattr(cli, "book_tee_time", _fake_booking(error))

    assert cli.main(["--date", "2026-10-03"]) == 1
    (row,) = _rows(log_path)
    assert row["status"].startswith("Error in booking process: RuntimeError: Login")
    assert "\n" not in row["status"]
    assert "Traceback" not in row["status"]
    assert len(row["status"]) <= len("Error in booking process: ") + 200


def test_brief_handles_empty_messages():
    assert cli._brief(ValueError()) == "ValueError"
    assert cli._brief(ValueError("bad\nworse")) == "ValueError: bad"


def test_date_flag_overrides_the_computed_target(log_path, monkeypatch):
    seen = {}

    def book_tee_time(course, criteria, settings, run_id):
        seen["date"] = criteria.target_date
        raise RuntimeError("stop")

    monkeypatch.setattr(cli, "book_tee_time", book_tee_time)
    cli.main(["--date", "2026-10-03"])
    assert seen["date"] == date(2026, 10, 3)
