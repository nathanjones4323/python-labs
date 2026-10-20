"""The sets logged on a recorded strength activity: reading them in the
canonical form, and replacing them.

The watch logs each set with the exercise the workout said was next, so a
workout done in a different order than it was written is logged under the
wrong exercises. The timeline (when each set started and stopped) is real;
the labels aren't. An edit relabels: the caller passes the whole list back
with names, reps and weights corrected, keeping each set's duration.

Garmin has one call for this, and it replaces every set at once, rests
included. Start times aren't taken from the caller: the sets are laid end
to end from the first logged set's start. Garmin keeps no history of the
old sets, so before writing, a copy of what Garmin had is saved under
`$GARMIN_STATE_DIR/activity-set-backups/`.
"""

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from garmin_mcp.errors import (
    InvalidInput,
    NotFound,
    Unsupported,
    WriteRejected,
)
from garmin_mcp.repositories import exercise_catalog
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.schemas.activity_sets import MAX_ACTIVITY_SETS, ActivitySet
from garmin_mcp.schemas.results import ChangePreview, Verification, WriteResult
from garmin_mcp.schemas.workouts import ExerciseMatch
from garmin_mcp.services.writes import Checked, WritePlan, WriteRunner

GRAMS_PER_LB = 453.59237
# A read gives durations to the millisecond; an agent passing them back may
# round. Weights are compared to 0.1 lb, as for workouts.
DURATION_TOLERANCE_SECONDS = 1.0
WEIGHT_TOLERANCE_KG = 0.05
# Garmin's set timestamps: UTC, whole seconds, one decimal.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.0"
_EXERCISE_ID = re.compile(r"[A-Z0-9_]+/[A-Z0-9_]*")

Resolver = Callable[[str | None, str | None], ExerciseMatch]


class ActivitySetService:
    def __init__(
        self,
        repo: GarminRepository,
        runner: WriteRunner,
        resolve: Resolver,
        backup_dir: Path,
    ) -> None:
        self._repo = repo
        self._runner = runner
        self._resolve = resolve
        self._backup_dir = backup_dir

    def update_exercise_sets(
        self,
        activity_id: int,
        sets: list[ActivitySet],
        *,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        plan = self._plan(activity_id, sets)
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    def _plan(self, activity_id: int, sets: list[ActivitySet]) -> WritePlan:
        if not 1 <= len(sets) <= MAX_ACTIVITY_SETS:
            raise InvalidInput(f"give 1–{MAX_ACTIVITY_SETS} sets")
        activity = self._repo.get_activity(activity_id)
        kind = activity.get("activityTypeDTO") or activity.get("activityType") or {}
        if kind.get("typeKey") != "strength_training":
            raise Unsupported(
                f"activity {activity_id} is a {kind.get('typeKey') or 'unknown'} "
                f"activity; only strength activities have exercise sets"
            )
        current_raw = self._repo.get_activity_exercise_sets(activity_id)
        current = read_sets(current_raw)
        start = _first_start(current_raw, activity)

        problems: list[str] = []
        matches: list[ExerciseMatch | None] = []
        for i, s in enumerate(sets, start=1):
            if s.type == "rest":
                matches.append(None)
                continue
            try:
                matches.append(self._resolve(s.name, s.exercise_id))
            except (NotFound, InvalidInput) as exc:
                problems.append(f"set {i}: {exc}")
                matches.append(None)
        if problems:
            raise NotFound("exercise", " | ".join(problems))

        payload = to_garmin(activity_id, sets, matches, start)
        intended = read_sets(payload)
        if not differences(intended, current):
            raise InvalidInput(
                f"those are already activity {activity_id}'s sets; nothing to change"
            )

        backup: list[Path] = []

        def execute() -> Any:
            backup.append(self._save_backup(activity_id, current_raw))
            return self._repo.set_activity_exercise_sets(activity_id, payload)

        def check() -> Checked:
            ids: dict[str, Any] = {"activity_id": activity_id}
            if backup:
                ids["backup_file"] = str(backup[0])
            actual = read_sets(self._repo.get_activity_exercise_sets(activity_id))
            checked = f"activity {activity_id}'s sets read back from Garmin"
            mismatches = differences(intended, actual)
            if not mismatches:
                return Verification(status="verified", checked=checked), ids
            if not differences(current, actual):
                mismatches.insert(0, "Garmin still has the old sets")
            return Verification(
                status="mismatch", checked=checked, mismatches=mismatches
            ), ids

        summary = activity.get("summaryDTO") or {}
        name = activity.get("activityName") or "untitled"
        day = str(summary.get("startTimeLocal") or "")[:10]
        return WritePlan(
            operation="update_activity_exercise_sets",
            preview=ChangePreview(
                summary=(
                    f"Replace the {len(current)} sets logged on activity "
                    f"{activity_id} ({name!r}, {day}) with these {len(intended)}"
                ),
                details=_side_by_side(current, intended),
                warnings=_warnings(current, intended, summary, start)
                + [
                    "Garmin keeps no history of logged sets. A copy of the "
                    f"current ones is saved under {self._backup_dir} first."
                ],
            ),
            basis={"payload": payload, "current": current_raw},
            execute=execute,
            verify=lambda _response: check(),
            reconcile=check,
            refs={"activity_id": activity_id},
        )

    def _save_backup(self, activity_id: int, current_raw: JSON) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._backup_dir / f"{activity_id}-{stamp}.json"
        try:
            self._backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(current_raw, f, indent=1)
        except OSError as exc:
            raise WriteRejected(
                f"Couldn't save a backup of the current sets ({exc}), so "
                f"nothing was sent to Garmin."
            ) from exc
        return path


# -- Garmin <-> canonical --------------------------------------------------


def read_sets(raw: JSON) -> list[ActivitySet]:
    return [_read_set(s) for s in (raw.get("exerciseSets") or [])[:MAX_ACTIVITY_SETS]]


def _read_set(s: JSON) -> ActivitySet:
    duration = max(_num(s.get("duration")) or 0.0, 0.0)
    if s.get("setType") == "REST":
        return ActivitySet(type="rest", duration_seconds=duration)
    first = (s.get("exercises") or [{}])[0]
    category, variant = first.get("category"), first.get("name")
    exercise_id = f"{category}/{variant or ''}" if category else None
    match = exercise_catalog.by_id(exercise_id) if exercise_id else None
    grams = _num(s.get("weight"))
    reps = s.get("repetitionCount")
    return ActivitySet(
        name=match.name if match else None,
        exercise_id=exercise_id
        if exercise_id and _EXERCISE_ID.fullmatch(exercise_id)
        else None,
        reps=reps if isinstance(reps, int) and not isinstance(reps, bool) else None,
        weight_kg=round(grams / 1000, 3) if grams and grams > 0 else None,
        duration_seconds=duration,
    )


def to_garmin(
    activity_id: int,
    sets: list[ActivitySet],
    matches: list[ExerciseMatch | None],
    start: datetime,
) -> JSON:
    out: list[JSON] = []
    offset = 0.0
    for index, (s, match) in enumerate(zip(sets, matches, strict=True)):
        entry: JSON = {
            "startTime": (start + timedelta(seconds=offset)).strftime(_TIME_FORMAT),
            "duration": s.duration_seconds,
            "messageIndex": index,
            "wktStepIndex": None,
        }
        if s.type == "rest" or match is None:
            entry |= {
                "setType": "REST",
                "exercises": [],
                "repetitionCount": None,
                "weight": None,
            }
        else:
            category, variant = match.exercise_id.split("/", 1)
            entry |= {
                "setType": "ACTIVE",
                "exercises": [
                    {
                        "category": category,
                        "name": variant or None,
                        "probability": 100.0,
                    }
                ],
                "repetitionCount": s.reps,
                "weight": _grams(s),
            }
        out.append(entry)
        offset += s.duration_seconds
    return {"activityId": activity_id, "exerciseSets": out}


def _grams(s: ActivitySet) -> float | None:
    if s.weight_lb:
        return float(round(s.weight_lb * GRAMS_PER_LB))
    if s.weight_kg:
        return float(round(s.weight_kg * 1000))
    return None


def _first_start(sets_raw: JSON, activity: JSON) -> datetime:
    """Where the rebuilt timeline begins: the first logged set, or failing
    that the activity's own start (both UTC)."""
    summary = activity.get("summaryDTO") or {}
    first: JSON = next(iter(sets_raw.get("exerciseSets") or []), {})
    for value in (
        first.get("startTime"),
        summary.get("startTimeGMT"),
        activity.get("startTimeGMT"),
    ):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace(" ", "T"))
            except ValueError:
                continue
    raise Unsupported("Garmin didn't say when this activity started")


# -- comparing and describing ----------------------------------------------


def differences(intended: list[ActivitySet], actual: list[ActivitySet]) -> list[str]:
    out = []
    if len(intended) != len(actual):
        out.append(f"{len(actual)} sets where {len(intended)} were expected")
    for i, (want, got) in enumerate(zip(intended, actual, strict=False), start=1):
        if not _same(want, got):
            out.append(f"set {i}: expected {describe(want)}; found {describe(got)}")
    return out


def _same(a: ActivitySet, b: ActivitySet) -> bool:
    return (
        (a.type, a.exercise_id, a.reps) == (b.type, b.exercise_id, b.reps)
        and abs(a.duration_seconds - b.duration_seconds) <= DURATION_TOLERANCE_SECONDS
        and abs(_kg(a) - _kg(b)) <= WEIGHT_TOLERANCE_KG
    )


def _kg(s: ActivitySet) -> float:
    if s.weight_lb:
        return s.weight_lb * GRAMS_PER_LB / 1000
    return s.weight_kg or 0.0


def describe(s: ActivitySet) -> str:
    if s.type == "rest":
        return f"Rest {_clock(s.duration_seconds)}"
    label = s.name or s.exercise_id or "unknown exercise"
    work = f"{s.reps} reps" if s.reps is not None else "reps not counted"
    kg = _kg(s)
    if kg:
        work += f" @ {round(kg * 1000 / GRAMS_PER_LB, 1):g} lb"
    return f"{label}: {work}, {_clock(s.duration_seconds)}"


def _side_by_side(current: list[ActivitySet], new: list[ActivitySet]) -> list[str]:
    lines = []
    for i in range(max(len(current), len(new))):
        old = current[i] if i < len(current) else None
        now = new[i] if i < len(new) else None
        if now is None:
            assert old is not None
            lines.append(f"{i + 1}. removed (was {describe(old)})")
        elif old is None:
            lines.append(f"{i + 1}. {describe(now)} (added)")
        elif _same(old, now):
            lines.append(f"{i + 1}. {describe(now)}")
        else:
            lines.append(f"{i + 1}. {describe(now)} (was {describe(old)})")
    return lines


def _warnings(
    current: list[ActivitySet],
    new: list[ActivitySet],
    summary: JSON,
    start: datetime,
) -> list[str]:
    out = []
    before = sum(s.duration_seconds for s in current)
    after = sum(s.duration_seconds for s in new)
    if abs(after - before) > DURATION_TOLERANCE_SECONDS:
        out.append(
            f"The sets now add up to {_clock(after)} (before: {_clock(before)}). "
            f"Start times are rebuilt end to end from {start:%H:%M:%S} UTC, so "
            f"every set after a changed duration moves."
        )
    length = _num(summary.get("duration"))
    if length and after > length + DURATION_TOLERANCE_SECONDS:
        out.append(
            f"The sets run {_clock(after - length)} past the end of the "
            f"activity ({_clock(length)} long)."
        )
    return out


def _clock(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _num(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )
