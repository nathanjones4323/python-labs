from datetime import date
from urllib.parse import parse_qs, urlsplit

from pay_my_bills.split import dollars, request_note, share_cents, venmo_request_url


def test_half_rounds_half_a_cent_up():
    assert share_cents(5719, 50) == 2860
    assert share_cents(8696, 50) == 4348


def test_other_percentages():
    assert share_cents(10000, 40) == 4000
    assert share_cents(101, 33) == 33


def test_dollars():
    assert dollars(2860) == "28.60"
    assert dollars(145000) == "1,450.00"
    assert dollars(5) == "0.05"


def test_venmo_link_prefills_the_request():
    note = request_note("Ventura Water", date(2026, 10, 7), 5719)
    assert note == "Ventura Water Oct 2026 ($57.19 total)"
    url = venmo_request_url("AGWill", 2860, note)
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "venmo.com"
    assert parse_qs(parts.query) == {
        "txn": ["charge"],
        "audience": ["private"],
        "recipients": ["AGWill"],
        "amount": ["28.60"],
        "note": [note],
    }
    # Spaces as %20: Venmo shows a literal "+" otherwise.
    assert "+" not in url


def test_venmo_amount_has_no_thousands_separator():
    assert "amount=1450.00" in venmo_request_url("AGWill", 145000, "Rent")
