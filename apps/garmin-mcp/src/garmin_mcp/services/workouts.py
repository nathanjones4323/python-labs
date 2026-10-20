"""Workouts: exercise lookup, listing, reading, creating and updating.

Creating and updating go through `WriteRunner`; this module builds the plan
for each: resolve every exercise against Garmin's catalog, translate to
Garmin's JSON, check for duplicates, preview the change, and say how to
read it back.
"""

import difflib
from typing import Any, TypeVar

from garmin_mcp.errors import Ambiguous, Conflict, InvalidInput, NotFound, Unsupported
from garmin_mcp.repositories import exercise_catalog
from garmin_mcp.repositories.garmin_client import JSON, GarminRepository
from garmin_mcp.repositories.workout_codec import (
    SPORT_KEYS,
    from_garmin,
    sport_key,
    to_garmin,
)
from garmin_mcp.schemas import workouts as w
from garmin_mcp.schemas.results import ChangePreview, Verification, WriteResult
from garmin_mcp.services.writes import Checked, WritePlan, WriteRunner

AnyWorkout = w.StrengthWorkout | w.RunningWorkout
Ex = TypeVar("Ex", w.StrengthExercise, w.CircuitExercise)

# How far back in the library to look for a same-named workout.
DUPLICATE_SCAN = 100


class WorkoutService:
    def __init__(self, repo: GarminRepository, runner: WriteRunner) -> None:
        self._repo = repo
        self._runner = runner

    # -- exercises --------------------------------------------------------

    def search_exercises(self, term: str, limit: int = 10) -> list[w.ExerciseMatch]:
        words = [exercise_catalog.normalize(t) for t in term.split()]
        words = [t for t in words if t]
        if not words:
            raise InvalidInput("search term is empty")
        whole = "".join(words)
        hits = [
            (e, name)
            for e, name in exercise_catalog.normalized_names()
            if all(t in name for t in words)
        ]

        def rank(hit: tuple[w.ExerciseMatch, str]) -> tuple[int, int, str]:
            e, name = hit
            return (
                0 if name == whole else 1 if name.startswith(whole) else 2,
                len(name),
                e.name,
            )

        return [e for e, _ in sorted(hits, key=rank)[:limit]]

    def resolve_exercises(self, workout: AnyWorkout) -> AnyWorkout:
        """Pin every exercise to a catalog entry, or refuse.

        A name must match a catalog name, ignoring case and punctuation.
        Nothing near-but-different is ever picked: a miss is an error listing
        the closest names, for the agent to choose from with the user.
        """
        if not isinstance(workout, w.StrengthWorkout):
            return workout
        problems: list[str] = []

        def pin(ex: Ex) -> Ex:
            try:
                match = self.resolve_exercise(ex.name, ex.exercise_id)
            except (NotFound, Ambiguous, InvalidInput) as exc:
                problems.append(str(exc))
                return ex
            return ex.model_copy(
                update={"name": match.name, "exercise_id": match.exercise_id}
            )

        items: list[w.StrengthExercise | w.Circuit] = [
            item.model_copy(update={"exercises": [pin(ex) for ex in item.exercises]})
            if isinstance(item, w.Circuit)
            else pin(item)
            for item in workout.exercises
        ]
        if problems:
            raise NotFound("exercise", " | ".join(problems))
        return workout.model_copy(update={"exercises": items})

    def resolve_exercise(
        self, name: str | None, exercise_id: str | None
    ) -> w.ExerciseMatch:
        """One exercise, by catalog name, by exercise_id, or by both when
        they agree. See `resolve_exercises`."""
        if exercise_id is not None:
            match = exercise_catalog.by_id(exercise_id)
            if match is None:
                raise NotFound(
                    "exercise", f"no catalog exercise has id {exercise_id!r}"
                )
            if name is not None and exercise_catalog.normalize(
                match.name
            ) != exercise_catalog.normalize(name):
                raise InvalidInput(
                    f"exercise_id {exercise_id} is {match.name!r}, not {name!r}"
                )
            return match
        if name is None:
            raise InvalidInput("an exercise needs a name or an exercise_id")
        key = exercise_catalog.normalize(name)
        matches = [e for e, n in exercise_catalog.normalized_names() if n == key]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(m.exercise_id for m in matches)
            raise Ambiguous(
                f"{name!r} matches several catalog entries ({ids}); pass exercise_id"
            )
        try:
            near = self.search_exercises(name, limit=5)
        except InvalidInput:
            near = []
        if not near:
            # Fall back to any single word, for "dumbbell bench" -> "Bench".
            for word in name.split():
                near = (
                    self.search_exercises(word, limit=5)
                    if exercise_catalog.normalize(word)
                    else []
                )
                if near:
                    break
        hint = f" Closest: {', '.join(repr(m.name) for m in near)}." if near else ""
        raise NotFound("exercise", f"{name!r} isn't in Garmin's catalog.{hint}")

    # -- reads ------------------------------------------------------------

    def list_workouts(self, sport: str | None, limit: int) -> list[w.WorkoutSummary]:
        keys = [SPORT_KEYS[sport]] if sport else None
        return [summarize(raw) for raw in self._repo.list_workouts(limit, keys)]

    def get_workout(self, workout_id: int) -> w.WorkoutView:
        raw = self._repo.get_workout(workout_id)
        view = w.WorkoutView(
            **summarize(raw).model_dump(), description=raw.get("description")
        )
        try:
            view.definition = from_garmin(raw)
        except Unsupported as exc:
            view.unsupported_reason = str(exc)
        return view

    # -- writes -----------------------------------------------------------

    def create_workout(
        self,
        workout: AnyWorkout,
        *,
        allow_duplicate_name: bool,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        plan = self._plan_create(workout, allow_duplicate_name)
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    def update_workout(
        self,
        workout_id: int,
        workout: AnyWorkout,
        *,
        dry_run: bool,
        confirmation_token: str | None,
    ) -> WriteResult:
        plan = self._plan_update(workout_id, workout)
        return self._runner.run(
            plan, dry_run=dry_run, confirmation_token=confirmation_token
        )

    def _plan_create(
        self, workout: AnyWorkout, allow_duplicate_name: bool
    ) -> WritePlan:
        resolved = self.resolve_exercises(workout)
        payload = to_garmin(resolved)
        intended = from_garmin(payload)
        sport = SPORT_KEYS[workout.sport]

        same_name = sorted(
            int(raw["workoutId"])
            for raw in self._repo.list_workouts(DUPLICATE_SCAN, [sport])
            if _same_name(raw.get("workoutName"), workout.name)
        )
        warnings: list[str] = []
        if same_name:
            if not allow_duplicate_name:
                raise Conflict(
                    f"A {workout.sport} workout named {workout.name!r} already "
                    f"exists (workout_id {', '.join(map(str, same_name))}). Use "
                    f"update_workout to change it, or pass "
                    f"allow_duplicate_name=true to create another."
                )
            warnings.append(f"Another workout with this name exists: {same_name}.")

        def verify(response: Any) -> Checked:
            workout_id = (
                response.get("workoutId") if isinstance(response, dict) else None
            )
            if not workout_id:
                return Verification(
                    status="not_found",
                    checked="Garmin's answer to the upload",
                    mismatches=["the answer had no workoutId"],
                ), {}
            return self._compare(int(workout_id), intended)

        def reconcile() -> Checked:
            new = [
                raw
                for raw in self._repo.list_workouts(20, [sport])
                if _same_name(raw.get("workoutName"), workout.name)
                and int(raw["workoutId"]) not in same_name
            ]
            if len(new) == 1:
                return self._compare(int(new[0]["workoutId"]), intended)
            return Verification(
                status="not_found" if not new else "mismatch",
                checked=f"the 20 most recently changed {sport} workouts",
                mismatches=[f"{len(new)} new workouts named {workout.name!r}"],
            ), {}

        return WritePlan(
            operation="create_workout",
            preview=ChangePreview(
                summary=f"Create {workout.sport} workout {resolved.name!r}",
                details=describe(resolved),
                warnings=warnings,
            ),
            basis={"payload": payload, "same_name": same_name},
            execute=lambda: self._repo.create_workout(payload),
            verify=verify,
            reconcile=reconcile,
        )

    def _plan_update(self, workout_id: int, workout: AnyWorkout) -> WritePlan:
        raw = self._repo.get_workout(workout_id)
        try:
            current = from_garmin(raw)
        except Unsupported as exc:
            raise Unsupported(
                f"workout {workout_id} can't be updated here without losing "
                f"parts of it ({exc}). Edit it in Garmin Connect instead."
            ) from exc
        if current.sport != workout.sport:
            raise InvalidInput(
                f"workout {workout_id} is a {current.sport} workout; an "
                f"update can't change the sport"
            )
        resolved = self.resolve_exercises(workout)
        payload = to_garmin(resolved)
        intended = from_garmin(payload)
        if intended == current:
            raise InvalidInput(
                f"that is already workout {workout_id}'s definition; nothing to change"
            )
        # Keep the ownership fields Garmin sent; the rest is the new definition.
        payload |= {k: raw[k] for k in ("ownerId", "author") if raw.get(k) is not None}

        def check() -> Checked:
            verification, ids = self._compare(workout_id, intended)
            if verification.status == "mismatch":
                again = from_garmin(self._repo.get_workout(workout_id))
                if again == current:
                    verification.mismatches.insert(
                        0, "Garmin still has the old definition"
                    )
            return verification, ids

        return WritePlan(
            operation="update_workout",
            preview=ChangePreview(
                summary=(
                    f"Replace the definition of workout {workout_id} ({current.name!r})"
                ),
                details=_diff(describe(current), describe(intended)),
                warnings=[
                    "Calendar entries for this workout keep pointing at it and "
                    "pick up the new definition."
                ],
            ),
            basis={"payload": payload, "current": current.model_dump(mode="json")},
            execute=lambda: self._repo.update_workout(workout_id, payload),
            verify=lambda _response: check(),
            reconcile=check,
            refs={"workout_id": workout_id},
        )

    def _compare(self, workout_id: int, intended: AnyWorkout) -> Checked:
        ids = {"workout_id": workout_id}
        raw = self._repo.get_workout(workout_id)
        checked = f"workout {workout_id} read back from Garmin"
        try:
            actual = from_garmin(raw)
        except Unsupported as exc:
            return Verification(
                status="mismatch", checked=checked, mismatches=[str(exc)]
            ), ids
        if actual == intended:
            return Verification(status="verified", checked=checked), ids
        return Verification(
            status="mismatch",
            checked=checked,
            mismatches=_diff(describe(intended), describe(actual)) or ["fields differ"],
        ), ids


def summarize(raw: JSON) -> w.WorkoutSummary:
    return w.WorkoutSummary(
        workout_id=int(raw["workoutId"]),
        name=raw.get("workoutName") or "",
        sport=sport_key(raw) or "unknown",
        created=raw.get("createdDate"),
        updated=raw.get("updatedDate") or raw.get("updateDate"),
        estimated_duration_seconds=raw.get("estimatedDurationInSecs"),
    )


def _same_name(a: Any, b: str) -> bool:
    return isinstance(a, str) and a.strip().casefold() == b.strip().casefold()


def _diff(old: list[str], new: list[str]) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(old, new, lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))
    ]


# -- human-readable previews ----------------------------------------------


def describe(workout: AnyWorkout) -> list[str]:
    lines = [f"Name: {workout.name}"]
    if workout.description:
        lines.append(f"Description: {workout.description}")
    if isinstance(workout, w.StrengthWorkout):
        for entry in workout.exercises:
            if isinstance(entry, w.Circuit):
                lines.append(f"Circuit, {entry.rounds} rounds:")
                lines += [f"  {_describe_exercise(ex)}" for ex in entry.exercises]
            else:
                lines.append(_describe_exercise(entry))
    else:
        for item in workout.steps:
            if isinstance(item, w.RepeatBlock):
                lines.append(f"Repeat {item.repeat_count}x:")
                lines += [f"  {_describe_step(s)}" for s in item.steps]
            else:
                lines.append(_describe_step(item))
    return lines


def _describe_exercise(ex: w.StrengthExercise | w.CircuitExercise) -> str:
    work = (
        f"{ex.reps} reps" if ex.reps is not None else _clock(ex.duration_seconds or 0)
    )
    if isinstance(ex, w.StrengthExercise):
        work = f"{ex.sets} x {work}"
    label = "" if ex.kind == "exercise" else f"{ex.kind.capitalize()}: "
    text = f"{label}{ex.name} [{ex.exercise_id}]: {work}"
    if ex.weight_lb is not None:
        text += f" @ {ex.weight_lb:g} lb"
    elif ex.weight_kg is not None:
        text += f" @ {ex.weight_kg:g} kg"
    text += f", rest {_clock(ex.rest_seconds)}" if ex.rest_seconds else ", no rest"
    if ex.notes:
        text += f" ({ex.notes})"
    return text


def _describe_step(step: w.RunStep) -> str:
    if step.duration_seconds is not None:
        length = _clock(step.duration_seconds)
    elif step.distance_meters is not None:
        length = f"{step.distance_meters:g} m"
    else:
        length = "until lap button"
    text = f"{step.type.capitalize()} {length}"
    match step.target:
        case w.PaceTarget() as p:
            text += f" @ {_clock(p.fast_seconds)}–{_clock(p.slow_seconds)}/{p.per}"
        case w.HeartRateTarget() as hr:
            text += f" @ {hr.min_bpm}–{hr.max_bpm} bpm"
        case w.HeartRateZoneTarget() as z:
            text += f" @ HR zone {z.zone}"
        case w.CadenceTarget() as c:
            text += f" @ {c.min_spm}–{c.max_spm} spm"
    if step.notes:
        text += f" ({step.notes})"
    return text


def _clock(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"
