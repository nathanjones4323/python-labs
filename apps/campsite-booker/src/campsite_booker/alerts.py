"""recreation.gov's own availability alert emails, read as data.

An alert you set up on recreation.gov ("Campsite Available!", from
communications@recreation.gov) names the campground, the site(s) with links
to them, the alert's date range, and, usefully, when recreation.gov saw the
opening ("As of Oct 27, 2026 at 4:03pm (PDT)"). The watcher uses one as a
trigger to check that campground at once, and to time itself against the
email (see `service.score_alert_emails`).

Pure functions over the email's HTML; `gmail.py` fetches it.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from zoneinfo import ZoneInfo

SENDER = "communications@recreation.gov"
# What the watcher searches Gmail for.
GMAIL_QUERY = f'from:{SENDER} subject:"Campsite Available"'

# The time zone abbreviations recreation.gov writes after "As of ...".
ZONES = {
    "PDT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "MDT": "America/Denver",
    "MST": "America/Denver",
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "EDT": "America/New_York",
    "EST": "America/New_York",
    "AKDT": "America/Anchorage",
    "AKST": "America/Anchorage",
    "HST": "Pacific/Honolulu",
}

AS_OF = re.compile(
    r"As of (\w{3} \d{1,2}, \d{4}) at (\d{1,2}:\d{2}\s*[ap]m)\s*\((\w+)\)", re.I
)
CAMPGROUND = re.compile(r"available to reserve at (.+?)\.\s*<", re.S)
# Site links only: the email's "nearby campgrounds" link to campgrounds.
CAMPSITE_LINK = re.compile(r"recreation\.gov/camping/campsites/(\d+)")
SITE_LABEL = re.compile(r"<strong>\s*([^<]+?),\s*</strong>")
DATE_RANGE = re.compile(
    r"<strong>\s*\w{3}, (\w{3} \d{1,2}, \d{4})\*?\s*-\s*\w{3}, (\w{3} \d{1,2}, \d{4})"
)
RECIPIENTS = re.compile(r"As many as ([\d,]+) other")


@dataclass(frozen=True)
class AvailabilityAlert:
    available_as_of: datetime | None
    campground_name: str | None
    campsite_ids: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    other_recipients: int | None = None


def _day(text: str) -> date:
    return datetime.strptime(text, "%b %d, %Y").date()


def _as_of(match: re.Match | None, fallback: ZoneInfo) -> datetime | None:
    if match is None:
        return None
    day, clock, zone = match.groups()
    naive = datetime.strptime(
        f"{day} {clock.replace(' ', '').upper()}", "%b %d, %Y %I:%M%p"
    )
    return naive.replace(tzinfo=ZoneInfo(ZONES.get(zone.upper(), str(fallback))))


def parse_alert(html: str, fallback_zone: ZoneInfo) -> AvailabilityAlert | None:
    """The alert in a "Campsite Available!" email's HTML, or None if it
    isn't one (a different recreation.gov email, or the format changed)."""
    if "Availability Alert" not in html:
        return None
    text = unescape(html)
    campsite_ids = list(dict.fromkeys(CAMPSITE_LINK.findall(text)))
    if not campsite_ids:
        return None
    dates = DATE_RANGE.search(text)
    campground = CAMPGROUND.search(text)
    recipients = RECIPIENTS.search(text)
    return AvailabilityAlert(
        available_as_of=_as_of(AS_OF.search(text), fallback_zone),
        campground_name=" ".join(campground.group(1).split()) if campground else None,
        campsite_ids=campsite_ids,
        sites=[s.strip() for s in SITE_LABEL.findall(text)],
        start_date=_day(dates.group(1)) if dates else None,
        end_date=_day(dates.group(2)) if dates else None,
        other_recipients=int(recipients.group(1).replace(",", ""))
        if recipients
        else None,
    )
