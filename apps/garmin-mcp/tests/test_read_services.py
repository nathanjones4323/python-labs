"""Activities and health metrics: normalization and request limits."""

from datetime import date, timedelta

import pytest
from garmin_mcp.errors import InvalidInput, NotFound
from garmin_mcp.server import App

from tests.fakes import FakeRepository

DAY = date(2026, 10, 15)


def add_activity(repo: FakeRepository, activity_id: int, kind: str, day: date) -> None:
    repo.activities.append(
        {
            "activityId": activity_id,
            "activityName": f"{kind} {activity_id}",
            "activityType": {"typeKey": kind},
            "startTimeLocal": f"{day.isoformat()} 07:00:00",
            "duration": 1800.0,
            "distance": 5000.0,
            "averageHR": 150.0,
            "trainingEffect": 3.1,
        }
    )


def test_recent_activities_filter_and_limit(app: App, repo: FakeRepository) -> None:
    for i in range(5):
        add_activity(
            repo,
            i + 1,
            "running" if i % 2 else "strength_training",
            DAY - timedelta(days=i),
        )
    runs = app.activities.recent(DAY - timedelta(days=7), DAY, "running", 10)
    assert [a.activity_id for a in runs] == [2, 4]
    assert runs[0].distance_meters == 5000.0
    assert len(app.activities.recent(DAY - timedelta(days=7), DAY, None, 3)) == 3
    # The limit goes to Garmin, not applied after fetching the whole range.
    assert repo.list_activities_limits[-1] == 3


def test_recent_activities_filter_by_subtype(app: App, repo: FakeRepository) -> None:
    # strength_training and trail_running are Garmin subtypes, which its
    # search refuses as activityType; the fake enforces that.
    add_activity(repo, 1, "strength_training", DAY)
    add_activity(repo, 2, "running", DAY)
    add_activity(repo, 3, "trail_running", DAY - timedelta(days=1))
    week = DAY - timedelta(days=7)
    lifts = app.activities.recent(week, DAY, "strength_training", 10)
    assert [a.activity_id for a in lifts] == [1]
    trail = app.activities.recent(week, DAY, "trail_running", 10)
    assert [a.activity_id for a in trail] == [3]
    # A top-level type takes in its subtypes, as on Garmin.
    runs = app.activities.recent(week, DAY, "running", 10)
    assert [a.activity_id for a in runs] == [2, 3]


def test_recent_activities_unknown_type(app: App) -> None:
    with pytest.raises(InvalidInput, match="unknown activity type"):
        app.activities.recent(DAY - timedelta(days=7), DAY, "lifting", 10)


@pytest.mark.parametrize(
    ("start", "end", "limit"),
    [
        (DAY, DAY - timedelta(days=1), 10),
        (DAY - timedelta(days=400), DAY, 10),
        (DAY, DAY, 0),
    ],
)
def test_recent_activities_limits(app: App, start: date, end: date, limit: int) -> None:
    with pytest.raises(InvalidInput):
        app.activities.recent(start, end, None, limit)


def test_strength_details_have_every_set(app: App, repo: FakeRepository) -> None:
    add_activity(repo, 1, "strength_training", DAY)
    details = app.activities.details(1)
    assert details.training_effect_aerobic == 3.1
    assert details.laps == []
    bench, rest = details.exercise_sets
    assert (bench.name, bench.exercise_id, bench.reps, bench.weight_kg) == (
        "Barbell Bench Press",
        "BENCH_PRESS/BARBELL_BENCH_PRESS",
        8,
        60.0,
    )
    assert (rest.type, rest.duration_seconds) == ("rest", 90.0)


def test_run_details_have_laps(app: App, repo: FakeRepository) -> None:
    add_activity(repo, 2, "running", DAY)
    details = app.activities.details(2)
    assert details.exercise_sets == []
    assert details.laps[0].distance_meters == 1000.0


def test_missing_activity(app: App) -> None:
    with pytest.raises(NotFound):
        app.activities.details(99)


def test_metrics_pick_headline_numbers(app: App, repo: FakeRepository) -> None:
    repo.metrics[("daily_summary", DAY)] = {
        "totalSteps": 9000,
        "restingHeartRate": 52,
        "lots": "more",
    }
    repo.metrics[("sleep", DAY)] = {
        "dailySleepDTO": {
            "sleepTimeSeconds": 27000,
            "sleepScores": {"overall": {"value": 81}},
        },
        "sleepLevels": [{"minute": "by minute"}],
    }
    repo.metrics[("training_readiness", DAY)] = [{"score": 70, "level": "MODERATE"}]
    result = app.health.metrics(DAY, None, None)
    values = result.days[0].values
    assert values["daily_summary"] == {"steps": 9000, "resting_hr_bpm": 52}
    assert values["sleep"] == {"sleep_seconds": 27000, "sleep_score": 81}
    assert values["training_readiness"] == {"readiness_score": 70, "level": "MODERATE"}


def test_metrics_missing_days_are_null(app: App) -> None:
    result = app.health.metrics(DAY, DAY + timedelta(days=1), ["hrv", "vo2max"])
    assert len(result.days) == 2
    assert result.days[0].values == {"hrv": None, "vo2max": None}


def test_metrics_only_fetch_what_was_asked(app: App, repo: FakeRepository) -> None:
    app.health.metrics(DAY, DAY + timedelta(days=2), ["hrv"])
    assert repo.reads == ["hrv"] * 3


@pytest.mark.parametrize(
    ("days", "metrics"),
    [(15, ["hrv"]), (14, ["hrv", "sleep", "vo2max", "training_status"])],
)
def test_metrics_limits(
    app: App, repo: FakeRepository, days: int, metrics: list[str]
) -> None:
    with pytest.raises(InvalidInput):
        app.health.metrics(DAY, DAY + timedelta(days=days - 1), metrics)  # type: ignore[arg-type]
    assert repo.reads == []
