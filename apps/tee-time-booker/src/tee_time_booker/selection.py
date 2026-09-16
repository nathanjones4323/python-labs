"""Choosing which of the available slots to book.

Platform-agnostic on purpose: adapters return `TeeTime`s, this picks one, so
every site applies the same preferences.
"""

from collections.abc import Collection
from datetime import datetime

from tee_time_booker.models import SearchCriteria, TeeTime


def choose_tee_time(
    tee_times: Collection[TeeTime],
    criteria: SearchCriteria,
    exclude: Collection[datetime] = (),
) -> TeeTime:
    """Pick the earliest (or latest) slot starting inside the criteria's window.

    `exclude` lists start times to skip, e.g. slots we already failed to hold.
    """
    candidates = [
        t
        for t in tee_times
        if criteria.earliest <= t.start.time() <= criteria.latest
        and t.start not in exclude
    ]
    if not candidates:
        raise ValueError(
            f"No tee times available between {criteria.earliest:%I:%M %p} "
            f"and {criteria.latest:%I:%M %p}. Try adjusting the time window."
        )
    candidates.sort(key=lambda t: t.start, reverse=not criteria.prefer_earlier)
    return candidates[0]
