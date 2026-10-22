"""One poll of Gmail, and the messages it leaves to send.

Synchronous: the bot runs both on a worker thread. `poll` fetches and records
new emails (one transaction each, so one bad message can't hold back the
rest) and then `schedule`s what the calendar says is due: assumed SCE
autopays and rent. `outbox` lists what the
person should be told, and `delivered` marks each item once Telegram took
it, so a failed send goes again on the next poll rather than being lost.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from pay_my_bills import messages, pipeline
from pay_my_bills.config import Settings
from pay_my_bills.db import Bill, ProcessedEmail, SplitRequest
from pay_my_bills.parsers import Email, parse

# How far back an ordinary poll looks. Generous, so a few days of the bot
# being off is caught up; anything already seen is skipped by id.
DEFAULT_LOOKBACK_DAYS = 7


class Mailbox(Protocol):
    def recent_ids(self, days: int) -> list[str]: ...
    def fetch(self, gmail_id: str) -> Email: ...


@dataclass(frozen=True)
class Outgoing:
    kind: str  # "request" | "reminder" | "notice" | "overdue"
    ref: int | str
    text: str
    # Requests and reminders carry the Venmo link and the buttons.
    venmo_url: str | None = None


def poll(
    sessions: sessionmaker[Session],
    mailbox: Callable[[], Mailbox],
    settings: Settings,
    now: datetime,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> int:
    """Record every new biller email from the last `days`; returns how many."""
    gmail = mailbox()
    ids = gmail.recent_ids(days)
    with sessions() as session:
        new = [
            gmail_id for gmail_id in ids if gmail_id not in pipeline.seen(session, ids)
        ]
    # Oldest first: a bill before its payment, a request before it's paid.
    emails = sorted(
        (gmail.fetch(gmail_id) for gmail_id in new), key=lambda e: e.received_at
    )
    for email in emails:
        with sessions.begin() as session:
            pipeline.record(
                session, email, parse(email, settings.timezone), settings, now
            )
    schedule(sessions, settings, now)
    return len(emails)


def schedule(
    sessions: sessionmaker[Session], settings: Settings, now: datetime
) -> None:
    """What's due by the calendar rather than by an email: assumed SCE
    autopays and rent. Runs after every poll, and on its own when Gmail
    can't be read, so rent still goes out on time."""
    with sessions.begin() as session:
        pipeline.assume_autopay(session, settings, now)
        pipeline.charge_rent(session, settings, now)


def outbox(
    sessions: sessionmaker[Session], settings: Settings, now: datetime
) -> list[Outgoing]:
    items: list[Outgoing] = []
    with sessions() as session:
        for notice in pipeline.pending_notices(session):
            items.append(Outgoing("notice", notice.gmail_id, notice.notice or ""))
        for bill in pipeline.overdue_bills(session, settings, now):
            items.append(Outgoing("overdue", bill.id, messages.overdue_text(bill)))
        for kind, requests in (
            ("request", pipeline.unsent_requests(session)),
            ("reminder", pipeline.due_reminders(session, now)),
        ):
            for request in requests:
                items.append(
                    Outgoing(
                        kind,
                        request.id,
                        messages.request_text(
                            request, settings, reminder=kind == "reminder"
                        ),
                        messages.venmo_url(request, settings),
                    )
                )
    return items


def delivered(sessions: sessionmaker[Session], item: Outgoing, now: datetime) -> None:
    with sessions.begin() as session:
        if item.kind == "notice":
            session.get_one(ProcessedEmail, item.ref).notified_at = now
        elif item.kind == "overdue":
            session.get_one(Bill, item.ref).overdue_alerted_at = now
        else:
            pipeline.mark_sent(
                session.get_one(SplitRequest, item.ref),
                now,
                reminder=item.kind == "reminder",
            )


def mark(
    sessions: sessionmaker[Session], request_id: int, action: str, now: datetime
) -> str:
    """A button press on a request: "done" (requested in Venmo), "paid" (they
    paid some way the app can't see: Zelle, cash, a request sent by hand) or
    "skip". Returns the request's state line afterwards."""
    with sessions.begin() as session:
        request = session.get(SplitRequest, request_id)
        if request is None:
            return "This request is gone."
        if action == "done" and request.requested_at is None:
            request.requested_at = now
        elif action == "paid" and request.settled_at is None:
            request.settled_at = now
        elif (
            action == "skip"
            and request.skipped_at is None
            and request.settled_at is None
        ):
            request.skipped_at = now
        return messages.request_state(request)


def status(sessions: sessionmaker[Session], settings: Settings) -> str:
    with sessions() as session:
        return messages.status_text(
            pipeline.open_requests(session), pipeline.unpaid_bills(session), settings
        )


def open_request_labels(sessions: sessionmaker[Session]) -> list[tuple[int, str]]:
    """(id, button label) for each open request, for /status's buttons."""
    with sessions() as session:
        return [
            (request.id, messages.short_label(request))
            for request in pipeline.open_requests(session)
        ]
