"""Editing the sets logged on a recorded strength activity: relabeling,
the rebuilt timeline, the backup, and the write flow against the fake."""

import json
from pathlib import Path
from typing import Any

import pytest
from garmin_mcp.config import Settings
from garmin_mcp.errors import (
    ApprovalInvalid,
    InvalidInput,
    NotFound,
    Unsupported,
    WriteOutcomeUnknown,
)
from garmin_mcp.schemas.activity_sets import ActivitySet
from garmin_mcp.schemas.results import WriteResult
from garmin_mcp.server import App
from pydantic import ValidationError

from tests.fakes import Failure, FakeRepository

ACTIVITY = 77
SQUAT = ("SQUAT", "BARBELL_BACK_SQUAT")
BENCH = ("BENCH_PRESS", "BARBELL_BENCH_PRESS")
LB_275 = 124750.0  # grams, as a watch logged it
LB_195 = 88437.0


def active(exercise: tuple[str, str], reps: int, grams: float, secs: float) -> Any:
    category, name = exercise
    return {
        "setType": "ACTIVE",
        "exercises": [{"category": category, "name": name, "probability": 99.6}],
        "repetitionCount": reps,
        "weight": grams,
        "duration": secs,
    }


def rest(secs: float) -> Any:
    return {"setType": "REST", "exercises": [], "duration": secs}


@pytest.fixture
def circuit_log(repo: FakeRepository) -> FakeRepository:
    """Squat, bench, squat as the watch logged a circuit; the lifter did
    squat, squat, bench."""
    repo.activities.append(
        {
            "activityId": ACTIVITY,
            "activityName": "5x5 Workout 1",
            "activityType": {"typeKey": "strength_training"},
            "startTimeLocal": "2026-10-19 15:41:16",
            "duration": 600.0,
        }
    )
    sets = [
        active(SQUAT, 5, LB_275, 60.5),
        rest(180.0),
        active(BENCH, 5, LB_195, 61.2),
        rest(180.0),
        active(SQUAT, 5, LB_275, 59.9),
    ]
    sets[0]["startTime"] = "2026-10-19T22:41:16.0"
    repo.exercise_sets[ACTIVITY] = {"activityId": ACTIVITY, "exerciseSets": sets}
    return repo


def squat(secs: float) -> dict[str, Any]:
    return {
        "name": "Barbell Back Squat",
        "reps": 5,
        "weight_lb": 275,
        "duration_seconds": secs,
    }


def bench(secs: float) -> dict[str, Any]:
    return {
        "name": "barbell bench press",
        "reps": 5,
        "weight_lb": 195,
        "duration_seconds": secs,
    }


def relabeled() -> list[dict[str, Any]]:
    return [
        squat(60.5),
        {"type": "rest", "duration_seconds": 180.0},
        squat(61.2),
        {"type": "rest", "duration_seconds": 180.0},
        bench(59.9),
    ]


def update(
    app: App, sets: list[dict[str, Any]], token: str | None = None
) -> WriteResult:
    return app.activity_sets.update_exercise_sets(
        ACTIVITY,
        [ActivitySet.model_validate(s) for s in sets],
        dry_run=False,
        confirmation_token=token,
    )


def approve_and_update(app: App, sets: list[dict[str, Any]]) -> WriteResult:
    preview = update(app, sets)
    assert preview.status == "approval_required"
    return update(app, sets, preview.confirmation_token)


def test_preview_shows_each_set_against_what_was_logged(
    app: App, circuit_log: FakeRepository
) -> None:
    result = update(app, relabeled())
    assert circuit_log.writes == []
    assert "5 sets logged on activity 77 ('5x5 Workout 1', 2026-10-19)" in (
        result.preview.summary
    )
    assert result.preview.details == [
        "1. Barbell Back Squat: 5 reps @ 275 lb, 1:00",
        "2. Rest 3:00",
        "3. Barbell Back Squat: 5 reps @ 275 lb, 1:01 "
        "(was Barbell Bench Press: 5 reps @ 195 lb, 1:01)",
        "4. Rest 3:00",
        "5. Barbell Bench Press: 5 reps @ 195 lb, 1:00 "
        "(was Barbell Back Squat: 5 reps @ 275 lb, 1:00)",
    ]
    assert any("no history" in w for w in result.preview.warnings)


def test_relabel_is_verified_and_keeps_the_timeline(
    app: App, circuit_log: FakeRepository, settings: Settings
) -> None:
    before = circuit_log.get_activity_exercise_sets(ACTIVITY)
    result = approve_and_update(app, relabeled())
    assert result.status == "verified", result.verification
    [(name, (activity_id, payload))] = circuit_log.writes
    assert (name, activity_id) == ("set_activity_exercise_sets", ACTIVITY)
    sent = payload["exerciseSets"]
    assert [s["startTime"] for s in sent] == [
        "2026-10-19T22:41:16.0",
        "2026-10-19T22:42:16.0",  # 60.5 s later, whole seconds
        "2026-10-19T22:45:16.0",
        "2026-10-19T22:46:17.0",
        "2026-10-19T22:49:17.0",
    ]
    assert sent[2]["exercises"] == [
        {"category": "SQUAT", "name": "BARBELL_BACK_SQUAT", "probability": 100.0}
    ]
    assert sent[2]["weight"] == 124738.0  # 275 lb in grams
    assert sent[1]["setType"] == "REST"

    backup = Path(result.result["backup_file"])  # type: ignore[index]
    assert backup.parent == settings.state_dir / "activity-set-backups"
    assert json.loads(backup.read_text()) == before
    assert backup.stat().st_mode & 0o777 == 0o600


def test_dropping_sets(app: App, circuit_log: FakeRepository) -> None:
    sets = relabeled()[:3]
    preview = update(app, sets)
    assert preview.preview.details[-2:] == [
        "4. removed (was Rest 3:00)",
        "5. removed (was Barbell Back Squat: 5 reps @ 275 lb, 1:00)",
    ]
    assert approve_and_update(app, sets).status == "verified"
    assert len(circuit_log.exercise_sets[ACTIVITY]["exerciseSets"]) == 3


def test_changed_durations_are_flagged(app: App, circuit_log: FakeRepository) -> None:
    sets = relabeled()
    sets[1]["duration_seconds"] = 400.0
    warnings = update(app, sets).preview.warnings
    assert any("now add up to 12:42 (before: 9:02)" in w for w in warnings)
    assert any("past the end of the activity" in w for w in warnings)


def test_same_sets_are_refused(app: App, circuit_log: FakeRepository) -> None:
    same = relabeled()
    same[2], same[4] = bench(61.2), squat(59.9)
    with pytest.raises(InvalidInput, match="nothing to change"):
        update(app, same)


def test_unknown_exercises_are_never_substituted(
    app: App, circuit_log: FakeRepository
) -> None:
    sets = relabeled()
    sets[4]["name"] = "Moon Press"
    with pytest.raises(NotFound, match="set 5: exercise: 'Moon Press'"):
        update(app, sets)
    assert circuit_log.writes == []


def test_only_strength_activities(app: App, repo: FakeRepository) -> None:
    repo.activities.append(
        {
            "activityId": ACTIVITY,
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-10-19 07:00:00",
        }
    )
    with pytest.raises(Unsupported, match="running"):
        update(app, relabeled())


def test_token_is_bound_to_the_logged_sets(
    app: App, circuit_log: FakeRepository
) -> None:
    preview = update(app, relabeled())
    circuit_log.exercise_sets[ACTIVITY]["exerciseSets"][0]["repetitionCount"] = 4
    with pytest.raises(ApprovalInvalid):
        update(app, relabeled(), preview.confirmation_token)
    assert circuit_log.writes == []


def test_read_back_mismatch_is_unknown(app: App, circuit_log: FakeRepository) -> None:
    circuit_log.mangle_sets = lambda stored: stored["exerciseSets"].pop()
    result = approve_and_update(app, relabeled())
    assert result.status == "unknown"
    assert result.verification
    assert "4 sets where 5 were expected" in result.verification.mismatches


def test_unapplied_write_says_old_sets_remain(
    app: App, circuit_log: FakeRepository
) -> None:
    original = circuit_log.get_activity_exercise_sets(ACTIVITY)
    circuit_log.mangle_sets = lambda stored: stored.update(original)
    result = approve_and_update(app, relabeled())
    assert result.status == "unknown"
    assert result.verification
    assert result.verification.mismatches[0] == "Garmin still has the old sets"


def test_timeout_that_landed_is_verified(app: App, circuit_log: FakeRepository) -> None:
    preview = update(app, relabeled())
    circuit_log.fail_next_write = Failure(WriteOutcomeUnknown("timed out"), apply=True)
    result = update(app, relabeled(), preview.confirmation_token)
    assert result.status == "verified"
    assert len(circuit_log.writes) == 1


def test_no_backup_no_write(
    app: App, circuit_log: FakeRepository, settings: Settings
) -> None:
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / "activity-set-backups").write_text("in the way")
    result = approve_and_update(app, relabeled())
    assert result.status == "failed"
    assert "backup" in (result.error or "")
    assert circuit_log.writes == []


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "rest", "name": "Plank", "duration_seconds": 30},
        {"name": "Plank", "weight_kg": 10, "weight_lb": 22, "duration_seconds": 30},
        {"name": "Plank"},
    ],
)
def test_set_shape(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ActivitySet.model_validate(bad)


def test_details_round_trip_into_an_update(
    app: App, circuit_log: FakeRepository
) -> None:
    # What get_activity_details returns is valid input, and unchanged it's
    # a no-op rather than a rewrite.
    logged = app.activities.details(ACTIVITY).exercise_sets
    with pytest.raises(InvalidInput, match="nothing to change"):
        app.activity_sets.update_exercise_sets(
            ACTIVITY, logged, dry_run=False, confirmation_token=None
        )


def test_uncataloged_and_unlabeled_sets_pass_back_unchanged(
    app: App, circuit_log: FakeRepository
) -> None:
    """A set the watch logged by category only, or with no exercise at all,
    reads with no name; passing it back as read must not block the edit."""
    logged = circuit_log.exercise_sets[ACTIVITY]["exerciseSets"]
    logged[2]["exercises"] = [{"category": "CURL", "name": None}]
    logged.append({**active(SQUAT, 5, 0, 30.0), "exercises": []})
    read = app.activities.details(ACTIVITY).exercise_sets
    assert read is not None
    assert (read[2].name, read[2].exercise_id) == (None, "CURL/")
    assert (read[5].name, read[5].exercise_id) == (None, None)
    sets = [s.model_dump(exclude_none=True) for s in read]
    sets[0]["reps"] = 6
    result = approve_and_update(app, sets)
    assert result.status == "verified", result
    written = circuit_log.exercise_sets[ACTIVITY]["exerciseSets"]
    assert written[2]["exercises"][0] == {
        "category": "CURL",
        "name": None,
        "probability": 100.0,
    }
    assert (written[5]["setType"], written[5]["exercises"]) == ("ACTIVE", [])


def test_activities_over_the_set_cap_are_refused(
    app: App, circuit_log: FakeRepository
) -> None:
    circuit_log.exercise_sets[ACTIVITY]["exerciseSets"] += [rest(1.0)] * 150
    with pytest.raises(Unsupported, match="more than 150"):
        update(app, relabeled())
