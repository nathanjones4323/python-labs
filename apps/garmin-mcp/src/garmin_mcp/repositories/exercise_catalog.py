"""Garmin's strength exercise catalog, as shipped with python-garminconnect.

1,500-odd exercises the Garmin Connect workout editor offers, each a
`category` (movement) and `exercise` (variant) pair. The pair is what a
workout step stores, so `CATEGORY/EXERCISE` is the id used here. The
catalog is static data in the library; nothing here calls Garmin.
"""

import re
from functools import cache

from garminconnect import exercises

from garmin_mcp.schemas.workouts import ExerciseMatch


@cache
def all_exercises() -> tuple[ExerciseMatch, ...]:
    return tuple(
        ExerciseMatch(
            exercise_id=f"{e['category']}/{e['exercise']}",
            name=e["name"],
            category=e["category"],
        )
        for e in exercises.EXERCISES
    )


@cache
def _by_id() -> dict[str, ExerciseMatch]:
    return {e.exercise_id: e for e in all_exercises()}


def by_id(exercise_id: str) -> ExerciseMatch | None:
    return _by_id().get(exercise_id)


@cache
def normalized_names() -> tuple[tuple[ExerciseMatch, str], ...]:
    """Each catalog exercise with its `normalize`d name, computed once."""
    return tuple((e, normalize(e.name)) for e in all_exercises())


def normalize(name: str) -> str:
    """Case, spacing and punctuation folded away: "push ups" == "Push-ups".
    Only spelling; never a different exercise."""
    return re.sub(r"[^a-z0-9]", "", name.lower())
