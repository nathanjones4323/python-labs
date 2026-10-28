import threading

from campsite_booker.booker import session_lock


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
