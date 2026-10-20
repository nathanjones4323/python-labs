"""Calendar reads, scheduling and rescheduling, plus pushing to a device."""

from datetime import date, timedelta
from typing import Any

import pytest
from garmin_mcp.errors import (
    Ambiguous,
    ApprovalInvalid,
    Conflict,
    InvalidInput,
    NotFound,
    Unsupported,
    WriteOutcomeUnknown,
)
from garmin_mcp.repositories.workout_codec import to_garmin
from garmin_mcp.schemas.results import WriteResult
from garmin_mcp.schemas.workouts import RunningWorkout
from garmin_mcp.server import App

from tests.conftest import running_spec
from tests.fakes import Failure, FakeRepository


@pytest.fixture
def workout_id(repo: FakeRepository) -> int:
    return repo.add_workout(to_garmin(RunningWorkout.model_validate(running_spec())))


def schedule(
    app: App, workout_id: int, day: date, token: str | None = None, **kw: Any
) -> WriteResult:
    return app.calendar.schedule_workout(
        workout_id,
        day,
        allow_duplicate=kw.get("allow_duplicate", False),
        dry_run=kw.get("dry_run", False),
        confirmation_token=token,
    )


def reschedule(
    app: App, to_date: date, token: str | None = None, **ref: Any
) -> WriteResult:
    return app.calendar.reschedule_workout(
        to_date,
        scheduled_workout_id=ref.get("scheduled_workout_id"),
        workout_id=ref.get("workout_id"),
        from_date=ref.get("from_date"),
        allow_duplicate=ref.get("allow_duplicate", False),
        dry_run=False,
        confirmation_token=token,
    )


def confirmed(call: Any, *args: Any, **kw: Any) -> WriteResult:
    preview = call(*args, **kw)
    assert preview.status == "approval_required", preview
    return call(*args, preview.confirmation_token, **kw)  # type: ignore[no-any-return]


# -- reads -----------------------------------------------------------------


def test_calendar_spans_months(app: App, repo: FakeRepository, workout_id: int) -> None:
    repo.add_calendar_entry(workout_id, date(2026, 10, 30))
    repo.add_calendar_entry(workout_id, date(2026, 11, 2))
    repo.add_calendar_entry(workout_id, date(2026, 11, 20))
    repo.calendar.append(
        {"id": 77, "itemType": "activity", "date": "2026-11-01", "title": "Morning Run"}
    )
    cal = app.calendar.get_calendar(date(2026, 10, 29), date(2026, 11, 5))
    assert [e.date for e in cal.scheduled_workouts] == [
        date(2026, 10, 30),
        date(2026, 11, 2),
    ]
    assert cal.scheduled_workouts[0].workout_id == workout_id
    assert [a.activity_id for a in cal.activities] == [77]


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2026, 10, 10), date(2026, 10, 9)), (date(2026, 1, 1), date(2026, 6, 1))],
)
def test_calendar_rejects_bad_ranges(app: App, start: date, end: date) -> None:
    with pytest.raises(InvalidInput):
        app.calendar.get_calendar(start, end)


# -- schedule --------------------------------------------------------------


def test_schedule_is_verified(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    result = confirmed(schedule, app, workout_id, tomorrow)
    assert result.status == "verified"
    entry_id = result.result["scheduled_workout_id"]  # type: ignore[index]
    assert [e["id"] for e in repo.calendar] == [entry_id]
    assert len(repo.writes) == 1


def test_schedule_preview_mentions_the_day(
    app: App, workout_id: int, tomorrow: date
) -> None:
    preview = schedule(app, workout_id, tomorrow)
    assert tomorrow.isoformat() in preview.preview.summary


@pytest.mark.parametrize("offset", [-1, 400])
def test_schedule_rejects_unlikely_dates(
    app: App, workout_id: int, offset: int
) -> None:
    with pytest.raises(InvalidInput):
        schedule(app, workout_id, date.today() + timedelta(days=offset))


def test_schedule_unknown_workout(app: App, tomorrow: date) -> None:
    with pytest.raises(NotFound) as err:
        schedule(app, 999, tomorrow)
    assert err.value.kind == "workout"


def test_same_workout_same_day_is_a_conflict(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    repo.add_calendar_entry(workout_id, tomorrow)
    with pytest.raises(Conflict, match="allow_duplicate"):
        schedule(app, workout_id, tomorrow)
    preview = schedule(app, workout_id, tomorrow, allow_duplicate=True)
    assert any("2 times" in w for w in preview.preview.warnings)
    assert repo.writes == []


def test_other_workouts_that_day_are_a_warning(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    other = repo.add_workout(
        to_garmin(RunningWorkout.model_validate(running_spec(name="Easy")))
    )
    repo.add_calendar_entry(other, tomorrow)
    preview = schedule(app, workout_id, tomorrow)
    assert any("Easy" in w for w in preview.preview.warnings)


def test_schedule_token_is_bound_to_the_calendar(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    preview = schedule(app, workout_id, tomorrow)
    other = repo.add_workout(
        to_garmin(RunningWorkout.model_validate(running_spec(name="Easy")))
    )
    repo.add_calendar_entry(other, tomorrow)  # the day changed after the preview
    with pytest.raises(ApprovalInvalid):
        schedule(app, workout_id, tomorrow, preview.confirmation_token)
    assert repo.writes == []


def test_schedule_unknown_outcome_reconciles(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    preview = schedule(app, workout_id, tomorrow)
    repo.fail_next_write = Failure(WriteOutcomeUnknown("timeout"))
    result = schedule(app, workout_id, tomorrow, preview.confirmation_token)
    assert result.status == "unknown"
    assert result.verification and result.verification.status == "not_found"
    assert len(repo.writes) == 1


# -- reschedule ------------------------------------------------------------


def test_reschedule_by_entry_id(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    entry = repo.add_calendar_entry(workout_id, tomorrow)
    later = tomorrow + timedelta(days=2)
    result = confirmed(reschedule, app, later, scheduled_workout_id=entry)
    assert result.status == "verified"
    assert repo.calendar[0]["date"] == later.isoformat()


def test_reschedule_by_workout_and_date(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    repo.add_calendar_entry(workout_id, tomorrow)
    later = tomorrow + timedelta(days=1)
    result = confirmed(
        reschedule, app, later, workout_id=workout_id, from_date=tomorrow
    )
    assert result.status == "verified"


def test_reschedule_ambiguous_reference(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    repo.add_calendar_entry(workout_id, tomorrow)
    repo.add_calendar_entry(workout_id, tomorrow)
    with pytest.raises(Ambiguous, match="scheduled_workout_id"):
        reschedule(
            app, tomorrow + timedelta(days=1), workout_id=workout_id, from_date=tomorrow
        )


@pytest.mark.parametrize(
    "ref",
    [
        {},
        {"workout_id": 1},
        {"scheduled_workout_id": 1, "workout_id": 1},
    ],
)
def test_reschedule_needs_one_clear_reference(
    app: App, tomorrow: date, ref: dict[str, Any]
) -> None:
    with pytest.raises(InvalidInput):
        reschedule(app, tomorrow, **ref)


def test_reschedule_missing_entry(app: App, workout_id: int, tomorrow: date) -> None:
    with pytest.raises(NotFound) as err:
        reschedule(app, tomorrow, workout_id=workout_id, from_date=tomorrow)
    assert err.value.kind == "calendar_entry"
    with pytest.raises(NotFound):
        reschedule(app, tomorrow, scheduled_workout_id=12345)


def test_reschedule_to_the_same_day(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    entry = repo.add_calendar_entry(workout_id, tomorrow)
    with pytest.raises(InvalidInput, match="already on"):
        reschedule(app, tomorrow, scheduled_workout_id=entry)


def test_reschedule_that_did_not_move_is_unknown(
    app: App,
    repo: FakeRepository,
    workout_id: int,
    tomorrow: date,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = repo.add_calendar_entry(workout_id, tomorrow)

    def stays_put(*_: Any) -> None:
        repo.writes.append(("reschedule_workout", None))

    monkeypatch.setattr(repo, "reschedule_workout", stays_put)
    result = confirmed(
        reschedule, app, tomorrow + timedelta(days=3), scheduled_workout_id=entry
    )
    assert result.status == "unknown"
    assert result.verification and result.verification.mismatches


# -- devices ---------------------------------------------------------------


def push(
    app: App, workout_id: int, device_id: int | None = None, token: str | None = None
) -> WriteResult:
    return app.devices.push_workout(
        workout_id, device_id, dry_run=False, confirmation_token=token
    )


def test_devices_mark_last_used(app: App) -> None:
    devices = app.devices.list_devices()
    assert [(d.device_id, d.last_used) for d in devices] == [
        (3001, True),
        (3002, False),
    ]


def test_push_defaults_to_last_used_and_reports_both_statuses(
    app: App, repo: FakeRepository, workout_id: int
) -> None:
    preview = push(app, workout_id)
    assert "Venu 4" in preview.preview.summary
    result = push(app, workout_id, token=preview.confirmation_token)
    assert result.status == "verified"
    assert result.result == {
        "workout_id": workout_id,
        "device_id": 3001,
        "upload_status": "accepted",
        "sync_status": "pending_on_garmin",
    }
    status = app.devices.push_status(workout_id, 3001)
    assert status.sync_status == "pending_on_garmin"


def test_push_not_seen_in_queue_is_unknown(
    app: App, repo: FakeRepository, workout_id: int
) -> None:
    repo.queue_pushes = False
    preview = push(app, workout_id, 3001)
    result = push(app, workout_id, 3001, preview.confirmation_token)
    assert result.status == "unknown"
    assert result.result and result.result["sync_status"] == "not_in_queue"


def test_push_to_unknown_device(app: App, workout_id: int) -> None:
    with pytest.raises(NotFound, match="3001 \\(Venu 4\\)"):
        push(app, workout_id, 9999)


def test_push_unknown_workout(app: App) -> None:
    with pytest.raises(NotFound) as err:
        push(app, 424242)
    assert err.value.kind == "workout"


def test_devices_report_workout_capabilities(app: App) -> None:
    venu, hrm = app.devices.list_devices()
    assert venu.workout_sports == {
        "strength_training": True,
        "running": True,
        "any": True,
    }
    assert venu.max_workouts == 200
    assert hrm.workout_sports == {}


def test_push_refuses_a_sport_the_device_cannot_run(
    app: App, repo: FakeRepository, workout_id: int
) -> None:
    repo.devices[0]["runningWorkoutCapable"] = False
    with pytest.raises(Unsupported, match="doesn't support running workouts"):
        push(app, workout_id, 3001)
    assert repo.writes == []


def test_push_refuses_a_device_without_workouts(
    app: App, repo: FakeRepository, workout_id: int
) -> None:
    repo.devices[1]["workoutCapable"] = False
    with pytest.raises(Unsupported, match="doesn't support workouts"):
        push(app, workout_id, 3002)


def test_push_warns_when_the_device_does_not_say(app: App, workout_id: int) -> None:
    preview = push(app, workout_id, 3002)
    assert preview.status == "approval_required"
    assert any("doesn't say" in w for w in preview.preview.warnings)


def test_push_preview_mentions_capacity(app: App, workout_id: int) -> None:
    preview = push(app, workout_id, 3001)
    assert preview.preview.warnings == []
    assert any("up to 200 workouts" in d for d in preview.preview.details)


def test_scheduling_queues_the_workout_like_garmin_does(
    app: App, workout_id: int, tomorrow: date
) -> None:
    confirmed(schedule, app, workout_id, tomorrow)
    assert app.devices.push_status(workout_id, 3001).sync_status == "pending_on_garmin"


def test_push_of_an_already_queued_workout_needs_a_new_message(
    app: App, repo: FakeRepository, workout_id: int, tomorrow: date
) -> None:
    # Seen live: scheduling made Garmin queue the workout before any push.
    confirmed(schedule, app, workout_id, tomorrow)
    preview = push(app, workout_id, 3001)
    assert any("already waiting" in w for w in preview.preview.warnings)

    # A push Garmin accepts but doesn't queue must not pass on the strength
    # of the message that was already there.
    repo.queue_pushes = False
    result = push(app, workout_id, 3001, preview.confirmation_token)
    assert result.status == "unknown"
    assert result.result and result.result["upload_status"] == "unknown"


def test_push_of_an_already_queued_workout_verifies_on_the_new_message(
    app: App, workout_id: int, tomorrow: date
) -> None:
    confirmed(schedule, app, workout_id, tomorrow)
    result = confirmed(push, app, workout_id, 3001)
    assert result.status == "verified"
    assert result.result and result.result["upload_status"] == "accepted"
