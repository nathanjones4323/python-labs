"""Which saw an opening first: the watcher, or recreation.gov's email.

All around the real alert: Upper Pines (232447) site 037 (campsite 94),
recreation.gov saying 4:03 PM PDT on Oct 27, the email reaching Gmail at
4:03:53 PM (23:03:53 UTC).
"""

from datetime import UTC, date, datetime, timedelta

from campsite_booker import service
from campsite_booker.availability import StayCriteria
from campsite_booker.db import (
    BY_EMAIL,
    EMAIL_FIRST,
    MISSED,
    NOT_WATCHED,
    WATCHER_FIRST,
    AlertEmail,
)
from campsite_booker.recgov import Campground
from conftest import FakeMailbox, alert_message, site

PINES = Campground(id="232447", name="Upper Pines Campground")
TODAY = date(2026, 10, 27)
OCT28 = date(2026, 10, 28)
RECEIVED = datetime(2026, 10, 27, 23, 3, 53, tzinfo=UTC)


def setup(db, recgov, settings, free=True):
    service.create_watch(db, PINES, StayCriteria(OCT28, OCT28, 1))
    recgov.sites[PINES.id] = [
        site("94", [OCT28] if free else [], site="037"),
        site("95", [], site="038"),
    ]
    mailbox = FakeMailbox()
    mailbox.messages["m1"] = alert_message("m1", RECEIVED)
    return mailbox


def record(db, recgov, settings, mailbox, now):
    return service.record_alert_emails(db, mailbox, recgov, settings.timezone, now)


def test_watcher_first(db, recgov, settings):
    mailbox = setup(db, recgov, settings)
    seen = RECEIVED - timedelta(seconds=33)
    service.check_campgrounds(db, recgov, seen, TODAY)

    now = RECEIVED + timedelta(seconds=10)
    [email] = record(db, recgov, settings, mailbox, now)
    assert (email.campground_id, email.campsite_ids, email.sites) == (
        PINES.id,
        ["94"],
        ["037"],
    )
    [decided] = service.score_alert_emails(db, now)
    assert decided.outcome == WATCHER_FIRST
    assert decided.lead_seconds == 33
    assert decided.watcher_seen_at == seen
    # Recorded once: the next look skips it.
    assert record(db, recgov, settings, mailbox, now) == []


def test_email_first_when_the_emails_check_finds_it(db, recgov, settings):
    mailbox = setup(db, recgov, settings)
    now = RECEIVED + timedelta(seconds=12)
    [email] = record(db, recgov, settings, mailbox, now)
    check = service.check_campgrounds(db, recgov, now, TODAY, {PINES.id}, BY_EMAIL)
    assert [a.found_by for a in check.alerts] == [BY_EMAIL]
    service.record_triggered_finds(db, email.gmail_id, 1)
    [decided] = service.score_alert_emails(db, now)
    assert decided.outcome == EMAIL_FIRST
    assert decided.lead_seconds == -12
    assert decided.triggered_finds == 1


def test_a_reopened_site_is_timed_from_when_it_came_back(db, recgov, settings):
    mailbox = setup(db, recgov, settings)
    service.check_campgrounds(db, recgov, RECEIVED - timedelta(hours=3), TODAY)
    recgov.sites[PINES.id][0].nights[OCT28] = "Reserved"
    service.check_campgrounds(db, recgov, RECEIVED - timedelta(hours=2), TODAY)
    recgov.sites[PINES.id][0].nights[OCT28] = "Available"
    back = RECEIVED - timedelta(seconds=5)
    service.check_campgrounds(db, recgov, back, TODAY)
    record(db, recgov, settings, mailbox, RECEIVED)
    [decided] = service.score_alert_emails(db, RECEIVED)
    assert (decided.outcome, decided.lead_seconds) == (WATCHER_FIRST, 5)


def test_missed_only_after_the_window_and_not_watched(db, recgov, settings):
    mailbox = setup(db, recgov, settings, free=False)
    record(db, recgov, settings, mailbox, RECEIVED)
    assert service.score_alert_emails(db, RECEIVED + timedelta(minutes=5)) == []
    [decided] = service.score_alert_emails(db, RECEIVED + timedelta(minutes=31))
    assert decided.outcome == MISSED

    mailbox.messages["m2"] = alert_message("m2", RECEIVED + timedelta(hours=1))
    for watch in service.list_watches(db):
        service.stop_watch(db, watch.id)
    record(db, recgov, settings, mailbox, RECEIVED + timedelta(hours=1))
    [decided] = service.score_alert_emails(db, RECEIVED + timedelta(hours=1))
    assert decided.outcome == NOT_WATCHED


def test_an_unreadable_email_is_recorded_and_not_refetched(db, recgov, settings):
    mailbox = FakeMailbox()
    message = alert_message("m1", RECEIVED)
    mailbox.messages["m1"] = type(message)("m1", "x", RECEIVED, "<p>nope</p>")
    [email] = record(db, recgov, settings, mailbox, RECEIVED)
    assert email.outcome == MISSED and email.campsite_ids == []
    assert record(db, recgov, settings, mailbox, RECEIVED) == []


def test_summary_and_log_line(db, recgov, settings):
    mailbox = setup(db, recgov, settings)
    service.check_campgrounds(db, recgov, RECEIVED - timedelta(seconds=40), TODAY)
    record(db, recgov, settings, mailbox, RECEIVED)
    [decided] = service.score_alert_emails(db, RECEIVED)
    line = service.timing_line(decided)
    assert line.startswith("TIMING outcome=watcher_first gmail_id=m1")
    assert "watcher_lead_s=40" in line
    # 4:03:00 PM per recreation.gov, in the inbox at 4:03:53.
    assert "email_delay_s=53" in line
    summary = service.timing_summary(db, RECEIVED - timedelta(days=1))
    assert summary.counts == {WATCHER_FIRST: 1}
    assert (summary.median_lead_s, summary.median_email_delay_s) == (40, 53)
    assert isinstance(summary.rows[0], AlertEmail)
