from dataclasses import replace
from datetime import date, datetime, time

import pytest
from tee_time_booker.models import SearchCriteria, TeeTime
from tee_time_booker.selection import choose_tee_time


def _criteria(earliest="08:00", latest="14:00", prefer_earlier=True) -> SearchCriteria:
    return SearchCriteria(
        target_date=date(2026, 10, 3),
        earliest=time.fromisoformat(earliest),
        latest=time.fromisoformat(latest),
        players=4,
        holes=18,
        prefer_earlier=prefer_earlier,
    )


def _slot(hhmm: str, fee: float = 50.0) -> TeeTime:
    return TeeTime(
        platform="test",
        course_name="Test Course",
        start=datetime.strptime(f"2026-10-03 {hhmm}", "%Y-%m-%d %H:%M"),
        holes=18,
        players=4,
        green_fee=fee,
    )


def test_ui_label_matches_how_booking_sites_render_times():
    assert _slot("08:00").ui_label == "8:00am"
    assert _slot("13:30").ui_label == "1:30pm"
    assert _slot("12:00").ui_label == "12:00pm"
    assert _slot("10:20").ui_label == "10:20am"


def test_choose_earliest_in_window():
    slots = [_slot("07:00"), _slot("14:10"), _slot("09:30"), _slot("08:20")]
    assert choose_tee_time(slots, _criteria()).start.time() == time(8, 20)


def test_choose_latest_in_window_when_not_preferring_earlier():
    slots = [_slot("07:00"), _slot("13:50"), _slot("09:30"), _slot("14:00")]
    chosen = choose_tee_time(slots, _criteria(prefer_earlier=False))
    assert chosen.start.time() == time(14, 0)


def test_window_is_inclusive():
    assert choose_tee_time([_slot("08:00")], _criteria()).start.time() == time(8, 0)


def test_exclude_skips_slots_already_tried():
    slots = [_slot("08:20"), _slot("09:30")]
    chosen = choose_tee_time(slots, _criteria(), exclude={slots[0]})
    assert chosen.start.time() == time(9, 30)


def test_exclude_is_per_slot_not_per_clock_time():
    front = replace(_slot("08:20"), side="Front")
    back = replace(_slot("08:20"), side="Back")
    chosen = choose_tee_time(
        [front, back, _slot("09:30")], _criteria(), exclude={front}
    )
    assert chosen == back


def test_no_slots_in_window_raises():
    with pytest.raises(ValueError, match="No tee times available"):
        choose_tee_time([_slot("06:00"), _slot("15:00")], _criteria())
