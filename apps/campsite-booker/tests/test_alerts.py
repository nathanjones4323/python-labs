import base64
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from campsite_booker.alerts import parse_alert
from campsite_booker.gmail import to_message
from conftest import FIXTURES

PACIFIC = ZoneInfo("America/Los_Angeles")
HTML = (FIXTURES / "alert_campsite_available.html").read_text()


def test_the_real_alert_reads():
    alert = parse_alert(HTML, PACIFIC)
    # "As of Oct 27, 2026 at 4:03pm (PDT)"
    assert alert.available_as_of == datetime(2026, 10, 27, 23, 3, tzinfo=UTC)
    assert alert.campground_name == "Upper Pines Campground"
    # The site's two links count once; the nearby-campground link not at all.
    assert alert.campsite_ids == ["94"]
    assert alert.sites == ["037"]
    assert (alert.start_date, alert.end_date) == (date(2026, 10, 28), date(2026, 11, 3))
    assert alert.other_recipients == 796


def test_other_time_zones_and_other_emails():
    eastern = HTML.replace("4:03pm (PDT)", "11:15am (EST)")
    assert parse_alert(eastern, PACIFIC).available_as_of == datetime(
        2026, 10, 27, 15, 15, tzinfo=UTC
    )
    assert parse_alert("<p>Your reservation is confirmed</p>", PACIFIC) is None
    no_sites = HTML.replace("camping/campsites/", "camping/x/")
    assert parse_alert(no_sites, PACIFIC) is None


def test_gmail_message_html_and_received_time():
    encoded = base64.urlsafe_b64encode(b"<p>hi</p>").decode().rstrip("=")
    message = {
        "id": "abc",
        "internalDate": "1793142233000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "Campsite Available!"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "eA"}},
                {"mimeType": "text/html", "body": {"data": encoded}},
            ],
        },
    }
    out = to_message(message)
    assert out.html == "<p>hi</p>"
    assert out.subject == "Campsite Available!"
    assert out.received_at == datetime(2026, 10, 27, 23, 3, 53, tzinfo=UTC)
