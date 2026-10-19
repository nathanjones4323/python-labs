"""Workout service: exercise resolution, and the create/update write flow
against the fake account."""

import json
from typing import Any

import pytest
from garmin_mcp.approval import ApprovalBook
from garmin_mcp.config import Settings
from garmin_mcp.errors import (
    ApprovalInvalid,
    Conflict,
    InvalidInput,
    NotFound,
    Unsupported,
    WriteOutcomeUnknown,
    WriteRejected,
)
from garmin_mcp.schemas.results import WriteResult
from garmin_mcp.schemas.workouts import RunningWorkout, StrengthWorkout, Workout
from garmin_mcp.server import App, build_app
from pydantic import TypeAdapter

from tests.conftest import running_spec, strength_spec
from tests.fakes import Failure, FakeRepository

workout_adapter: TypeAdapter[StrengthWorkout | RunningWorkout] = TypeAdapter(Workout)


def parse(spec: dict[str, Any]) -> StrengthWorkout | RunningWorkout:
    return workout_adapter.validate_python(spec)


def create(
    app: App, spec: dict[str, Any], token: str | None = None, **kw: Any
) -> WriteResult:
    return app.workouts.create_workout(
        parse(spec),
        allow_duplicate_name=kw.pop("allow_duplicate_name", False),
        dry_run=kw.pop("dry_run", False),
        confirmation_token=token,
    )


def approve_and_create(app: App, spec: dict[str, Any], **kw: Any) -> WriteResult:
    preview = create(app, spec, **kw)
    assert preview.status == "approval_required"
    return create(app, spec, preview.confirmation_token, **kw)


# -- exercises -------------------------------------------------------------


def test_search_exercises(app: App) -> None:
    hits = app.workouts.search_exercises("barbell bench press")
    assert hits[0].name == "Barbell Bench Press"
    assert hits[0].exercise_id == "BENCH_PRESS/BARBELL_BENCH_PRESS"
    assert all("bench" in h.name.lower() for h in hits)


def test_resolution_folds_spelling_only(app: App) -> None:
    resolved = app.workouts.resolve_exercises(parse(strength_spec()))
    assert isinstance(resolved, StrengthWorkout)
    assert [e.exercise_id for e in resolved.all_exercises()] == [
        "BENCH_PRESS/BARBELL_BENCH_PRESS",
        "PUSH_UP/PUSH_UP",
        "PLANK/PLANK",
    ]
    assert resolved.all_exercises()[1].name == "Push-up"


def test_unknown_exercise_is_never_substituted(app: App, repo: FakeRepository) -> None:
    spec = strength_spec(exercises=[{"name": "bench pres", "sets": 3, "reps": 5}])
    with pytest.raises(NotFound) as err:
        create(app, spec)
    assert err.value.kind == "exercise"
    assert "isn't in Garmin's catalog" in str(err.value)
    assert repo.writes == []


def test_unknown_exercise_lists_close_names(app: App) -> None:
    spec = strength_spec(exercises=[{"name": "dumbbell zottman", "sets": 3, "reps": 5}])
    with pytest.raises(NotFound, match="Closest:"):
        app.workouts.resolve_exercises(parse(spec))


def test_exercise_id_must_match_name(app: App) -> None:
    spec = strength_spec(
        exercises=[
            {
                "name": "Squat",
                "exercise_id": "BENCH_PRESS/BARBELL_BENCH_PRESS",
                "sets": 3,
                "reps": 5,
            }
        ]
    )
    with pytest.raises(NotFound, match="is 'Barbell Bench Press', not 'Squat'"):
        app.workouts.resolve_exercises(parse(spec))


def test_unknown_exercise_id(app: App) -> None:
    spec = strength_spec(
        exercises=[{"name": "Squat", "exercise_id": "SQUAT/NOPE", "sets": 3, "reps": 5}]
    )
    with pytest.raises(NotFound, match="no catalog exercise has id"):
        app.workouts.resolve_exercises(parse(spec))


# -- create: approval and dry run ------------------------------------------


def test_first_call_only_previews(app: App, repo: FakeRepository) -> None:
    result = create(app, strength_spec())
    assert result.status == "approval_required"
    assert result.confirmation_token
    assert result.preview.summary == "Create strength workout 'Push day'"
    assert any(
        "Barbell Bench Press" in line and "4 x 8 reps @ 135 lb" in line
        for line in result.preview.details
    )
    assert repo.writes == []


def test_confirmed_create_is_verified(app: App, repo: FakeRepository) -> None:
    result = approve_and_create(app, strength_spec())
    assert result.status == "verified", result
    assert result.verification and result.verification.status == "verified"
    workout_id = result.result["workout_id"]  # type: ignore[index]
    assert workout_id in repo.workouts
    assert len(repo.writes) == 1


def test_running_create_is_verified(app: App) -> None:
    assert approve_and_create(app, running_spec()).status == "verified"


def test_dry_run_never_writes(app: App, repo: FakeRepository) -> None:
    preview = create(app, strength_spec())
    result = create(app, strength_spec(), preview.confirmation_token, dry_run=True)
    assert result.status == "dry_run"
    assert repo.writes == []


def test_global_dry_run_blocks_even_with_a_token(
    settings: Settings, repo: FakeRepository
) -> None:
    app = build_app(settings.model_copy(update={"dry_run": True}), repo)
    first = create(app, strength_spec())
    assert first.status == "dry_run"
    assert first.confirmation_token is None
    assert "GARMIN_DRY_RUN" in first.message
    # A token from somewhere else doesn't get through either.
    other = build_app(settings, FakeRepository())
    token = create(other, strength_spec()).confirmation_token
    assert create(app, strength_spec(), token).status == "dry_run"
    assert repo.writes == []


def test_token_is_single_use(app: App, repo: FakeRepository) -> None:
    preview = create(app, strength_spec())
    assert create(app, strength_spec(), preview.confirmation_token).status == "verified"
    with pytest.raises(ApprovalInvalid, match="already used"):
        create(
            app, strength_spec(), preview.confirmation_token, allow_duplicate_name=True
        )
    assert len(repo.writes) == 1


def test_token_is_bound_to_the_previewed_change(app: App, repo: FakeRepository) -> None:
    preview = create(app, strength_spec())
    changed = strength_spec(name="Pull day")
    with pytest.raises(ApprovalInvalid, match="doesn't match"):
        create(app, changed, preview.confirmation_token)
    assert repo.writes == []


def test_garbage_token(app: App) -> None:
    with pytest.raises(ApprovalInvalid):
        create(app, strength_spec(), "not-a-token")


def test_tokens_expire() -> None:
    now = [1_000_000.0]
    book = ApprovalBook(600, clock=lambda: now[0])
    token, _ = book.issue("create_workout", "abc")
    now[0] += 601
    with pytest.raises(ApprovalInvalid, match="expired"):
        book.consume(token, "create_workout", "abc")


def test_tokens_are_bound_to_the_operation() -> None:
    book = ApprovalBook(600)
    token, _ = book.issue("create_workout", "abc")
    with pytest.raises(ApprovalInvalid):
        book.consume(token, "update_workout", "abc")
    book.consume(token, "create_workout", "abc")


# -- create: conflicts and outcomes ----------------------------------------


def test_duplicate_name_is_a_conflict(app: App, repo: FakeRepository) -> None:
    approve_and_create(app, strength_spec())
    with pytest.raises(Conflict, match="allow_duplicate_name"):
        create(app, strength_spec(name="push DAY"))
    result = create(app, strength_spec(), allow_duplicate_name=True)
    assert result.status == "approval_required"
    assert result.preview.warnings


def test_rejected_write_fails(app: App, repo: FakeRepository) -> None:
    preview = create(app, strength_spec())
    repo.fail_next_write = Failure(
        WriteRejected("Garmin refused the change: API Error 400")
    )
    result = create(app, strength_spec(), preview.confirmation_token)
    assert result.status == "failed"
    assert "400" in (result.error or "")
    assert repo.workouts == {}


def test_unknown_outcome_is_not_retried(app: App, repo: FakeRepository) -> None:
    preview = create(app, strength_spec())
    repo.fail_next_write = Failure(WriteOutcomeUnknown("ReadTimeout"))
    result = create(app, strength_spec(), preview.confirmation_token)
    assert result.status == "unknown"
    assert "Do NOT retry" in result.message
    assert len(repo.writes) == 1
    # And the token can't be used to try again.
    with pytest.raises(ApprovalInvalid):
        create(app, strength_spec(), preview.confirmation_token)
    assert len(repo.writes) == 1


def test_unknown_outcome_that_landed_is_verified_by_reading(
    app: App, repo: FakeRepository
) -> None:
    preview = create(app, strength_spec())
    repo.fail_next_write = Failure(WriteOutcomeUnknown("ReadTimeout"), apply=True)
    result = create(app, strength_spec(), preview.confirmation_token)
    assert result.status == "verified"
    assert "errored" in result.message
    assert len(repo.writes) == 1


def test_read_back_mismatch_is_unknown(app: App, repo: FakeRepository) -> None:
    def drop_weight(stored: dict[str, Any]) -> None:
        stored["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0].pop(
            "weightValue"
        )

    repo.mangle = drop_weight
    result = approve_and_create(app, strength_spec())
    assert result.status == "unknown"
    assert result.verification and result.verification.status == "mismatch"
    assert any("135 lb" in m for m in result.verification.mismatches)


# -- reads and updates -----------------------------------------------------


def test_get_workout_round_trips(app: App) -> None:
    workout_id = approve_and_create(app, running_spec()).result["workout_id"]  # type: ignore[index]
    view = app.workouts.get_workout(workout_id)
    assert view.definition == parse(running_spec())
    assert view.sport == "running"


def test_get_workout_explains_unsupported(app: App, repo: FakeRepository) -> None:
    workout_id = repo.add_workout(
        {
            "workoutName": "Ride",
            "sportType": {"sportTypeKey": "cycling"},
            "workoutSegments": [{}],
        }
    )
    view = app.workouts.get_workout(workout_id)
    assert view.definition is None
    assert "cycling" in (view.unsupported_reason or "")


def test_get_missing_workout(app: App) -> None:
    with pytest.raises(NotFound) as err:
        app.workouts.get_workout(1)
    assert err.value.kind == "workout"


def update(
    app: App, workout_id: int, spec: dict[str, Any], token: str | None = None
) -> WriteResult:
    return app.workouts.update_workout(
        workout_id, parse(spec), dry_run=False, confirmation_token=token
    )


def test_update_previews_a_diff_and_verifies(app: App, repo: FakeRepository) -> None:
    workout_id = approve_and_create(app, strength_spec()).result["workout_id"]  # type: ignore[index]
    heavier = strength_spec()
    heavier["exercises"][0]["weight_lb"] = 145
    preview = update(app, workout_id, heavier)
    assert preview.status == "approval_required"
    assert any(
        line.startswith("-") and "135 lb" in line for line in preview.preview.details
    )
    assert any(
        line.startswith("+") and "145 lb" in line for line in preview.preview.details
    )
    result = update(app, workout_id, heavier, preview.confirmation_token)
    assert result.status == "verified"
    stored = repo.workouts[workout_id]
    assert stored["workoutId"] == workout_id
    assert stored["ownerId"] == 42


def test_update_is_bound_to_the_current_state(app: App, repo: FakeRepository) -> None:
    workout_id = approve_and_create(app, strength_spec()).result["workout_id"]  # type: ignore[index]
    heavier = strength_spec()
    heavier["exercises"][0]["weight_lb"] = 145
    preview = update(app, workout_id, heavier)
    # Someone edits the workout in Garmin Connect in between.
    repo.workouts[workout_id]["workoutName"] = "Edited on the web"
    with pytest.raises(ApprovalInvalid):
        update(app, workout_id, heavier, preview.confirmation_token)


def test_update_without_changes(app: App) -> None:
    workout_id = approve_and_create(app, strength_spec()).result["workout_id"]  # type: ignore[index]
    with pytest.raises(InvalidInput, match="nothing to change"):
        update(app, workout_id, strength_spec())


def test_update_cannot_change_sport(app: App) -> None:
    workout_id = approve_and_create(app, strength_spec()).result["workout_id"]  # type: ignore[index]
    with pytest.raises(InvalidInput, match="sport"):
        update(app, workout_id, running_spec())


def test_update_refuses_workouts_it_would_damage(
    app: App, repo: FakeRepository
) -> None:
    workout_id = approve_and_create(app, running_spec()).result["workout_id"]  # type: ignore[index]
    steps = repo.workouts[workout_id]["workoutSegments"][0]["workoutSteps"]
    steps[0]["targetType"]["workoutTargetTypeKey"] = "power.zone"
    with pytest.raises(Unsupported, match="Edit it in Garmin Connect"):
        update(app, workout_id, running_spec(name="Renamed"))


# -- operation log ---------------------------------------------------------


def test_operation_log_holds_ids_not_content(app: App, settings: Settings) -> None:
    result = approve_and_create(app, strength_spec())
    lines = (settings.state_dir / "operations.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["operation"] == "create_workout"
    assert entry["status"] == "verified"
    assert entry["workout_id"] == str(result.result["workout_id"])  # type: ignore[index]
    assert "Bench" not in lines[-1]
    assert result.confirmation_token is None
    mode = (settings.state_dir / "operations.jsonl").stat().st_mode & 0o777
    assert mode == 0o600
