import threading
from datetime import date

from campsite_booker import booker
from campsite_booker.booker import session_lock
from campsite_booker.db import FAILED
from campsite_booker.recgov import Stay


def test_one_browser_run_at_a_time_on_the_session(settings):
    order: list[str] = []
    first_in = threading.Event()

    def second() -> None:
        first_in.wait()
        with session_lock(settings):
            order.append("second")

    other = threading.Thread(target=second)
    other.start()
    with session_lock(settings):
        first_in.set()
        other.join(timeout=0.3)
        # Still waiting on the lock this block holds.
        assert other.is_alive()
        order.append("first")
    other.join(timeout=5)
    assert order == ["first", "second"]


def test_a_state_park_site_isnt_held(settings, monkeypatch):
    def no_browser():
        raise AssertionError("opened a browser")

    monkeypatch.setattr(booker, "sync_playwright", no_browser)
    stay = Stay("rc-1001-2001", "50001", "001", "", "CAMPSITE", 0, date(2026, 11, 6), 2)
    result = booker.hold(settings, stay)
    assert result.status == FAILED
    assert "ReserveCalifornia" in result.detail
    assert result.page_url == "https://www.reservecalifornia.com/park/1001/2001"
