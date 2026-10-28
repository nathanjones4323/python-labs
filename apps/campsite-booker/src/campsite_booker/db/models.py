"""The tables: what you're watching for, what's turned up, and what was held.

| Table | Row |
| --- | --- |
| `watches` | A standing alert: this campground (or these sites), these dates |
| `openings` | One stay a watch found free, from first seen until it's gone |
| `holds` | One attempt to put a site in the recreation.gov cart, and how it ended |
| `alert_emails` | A recreation.gov alert email, and who saw its opening first |
"""

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from campsite_booker.availability import StayCriteria

# Hold.status
HELD = "held"
FAILED = "failed"
# The click went through but neither the cart nor an error showed up: look
# at the cart on recreation.gov.
UNKNOWN = "unknown"
# recreation.gov asked for a human check (reCAPTCHA) after Add to Cart, so it
# isn't in the cart: you add it on recreation.gov, then it can be paid from
# the cart.
CHALLENGED = "challenged"

# Hold.checkout_status (FAILED and UNKNOWN as above)
PAID = "paid"
# Went as far as "Review and Confirm" and stopped: CAMPSITE_SUBMIT_PAYMENT
# is off. Nothing was paid; the site is still in the cart.
DRY_RUN = "dry_run"

# Opening.found_by: what kind of check first saw it
BY_POLL = "poll"  # the regular check
BY_RELEASE = "release"  # a release-day burst
BY_EMAIL = "email"  # a check started by recreation.gov's alert email

# AlertEmail.outcome
WATCHER_FIRST = "watcher_first"
EMAIL_FIRST = "email_first"
# Covered by a watch, but the watcher never saw the site open: it went
# before a check, or the alert's filters are wider than the watch's.
MISSED = "missed"
# No active watch at that campground.
NOT_WATCHED = "not_watched"

# Watch.ended_reason
EXPIRED = "expired"  # every arrival date has passed
BOOKED = "booked"  # you said you booked it
STOPPED = "stopped"  # you turned it off


class UTCDateTime(TypeDecorator):
    """A timezone-aware datetime, stored as naive UTC.

    SQLite drops the offset of a `DateTime(timezone=True)`, so what comes back
    would be naive and fail every comparison with an aware `now`.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"Naive datetime {value!r}; pass an aware one")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    """A campground (or some of its sites) and the stay you want there.

    Checked every poll while `active`. `auto_hold` puts the first stay that
    opens up straight into the cart, once; after that the watch keeps
    alerting with a Hold button, so an unpaid hold isn't grabbed again every
    15 minutes.
    """

    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    campground_id: Mapped[str] = mapped_column(String(20))
    campground_name: Mapped[str] = mapped_column(String(200))
    # Empty: any site at the campground.
    campsite_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    arrival_from: Mapped[date]
    arrival_to: Mapped[date]
    nights: Mapped[int]
    # 0 = Monday ... 6 = Sunday; empty: any day.
    arrival_weekdays: Mapped[list[int]] = mapped_column(JSON, default=list)
    group_size: Mapped[int | None]
    site_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    include_group_sites: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    # Pay for the automatic hold too, with the card saved on the account.
    # Needs CAMPSITE_SUBMIT_PAYMENT as well; either off stops before paying.
    auto_pay: Mapped[bool] = mapped_column(Boolean, default=False)
    # The release the watch is waiting on, once it's been announced.
    armed_release_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ended_reason: Mapped[str | None] = mapped_column(String(20))
    # The one automatic hold has been tried (whether or not it worked).
    auto_held_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # The last check's error, cleared by the next good one.
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    openings: Mapped[list["Opening"]] = relationship(
        back_populates="watch", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def criteria(self) -> StayCriteria:
        return StayCriteria(
            arrival_from=self.arrival_from,
            arrival_to=self.arrival_to,
            nights=self.nights,
            arrival_weekdays=frozenset(self.arrival_weekdays or ()),
            group_size=self.group_size,
            campsite_ids=frozenset(self.campsite_ids or ()),
            site_types=tuple(self.site_types or ()),
            include_group_sites=self.include_group_sites,
        )


class Opening(Base):
    """A stay a watch found free. Seen again on later polls it's the same
    row; when it stops being free `gone_at` is set, and if it comes back the
    row is reopened and alerted again (someone's hold expired)."""

    __tablename__ = "openings"
    __table_args__ = (
        UniqueConstraint("watch_id", "campsite_id", "check_in", "nights"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    campsite_id: Mapped[str] = mapped_column(String(20))
    site: Mapped[str] = mapped_column(String(50))
    loop: Mapped[str] = mapped_column(String(100))
    campsite_type: Mapped[str] = mapped_column(String(100))
    max_people: Mapped[int]
    check_in: Mapped[date]
    nights: Mapped[int]
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # When it last went from not free to free: first_seen_at, or when it
    # came back. The time to compare with an alert email.
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    found_by: Mapped[str | None] = mapped_column(String(20))
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    gone_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    watch: Mapped[Watch] = relationship(back_populates="openings")


class Hold(Base):
    """One attempt to put a site in the cart, and to pay for it if asked. A
    held site is yours for recreation.gov's cart timer (15 minutes)."""

    __tablename__ = "holds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int | None] = mapped_column(
        ForeignKey("watches.id", ondelete="SET NULL")
    )
    campground_id: Mapped[str] = mapped_column(String(20))
    campground_name: Mapped[str] = mapped_column(String(200))
    campsite_id: Mapped[str] = mapped_column(String(20))
    site: Mapped[str] = mapped_column(String(50))
    check_in: Mapped[date]
    check_out: Mapped[date]
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(Text)
    # Where the browser ended up: the order-details page for a hold.
    page_url: Mapped[str | None] = mapped_column(String(500))
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    # How paying for it went, if it was tried: PAID, DRY_RUN, FAILED, UNKNOWN.
    checkout_status: Mapped[str | None] = mapped_column(String(20))
    checkout_detail: Mapped[str | None] = mapped_column(Text)
    # The order number recreation.gov showed, once paid.
    confirmation: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AlertEmail(Base):
    """A recreation.gov availability alert from Gmail. It starts a check of
    its campground at once, and records which saw the opening first: the
    watcher (`watcher_seen_at`, from the matching opening) or the email
    (`received_at`). `lead_seconds` is how far the watcher was ahead:
    positive, it won; negative, the email did."""

    __tablename__ = "alert_emails"

    gmail_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # When Gmail received it.
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # recreation.gov's own "As of Oct 27, 2026 at 4:03pm (PDT)", to the minute.
    available_as_of: Mapped[datetime | None] = mapped_column(UTCDateTime())
    campground_name: Mapped[str | None] = mapped_column(String(200))
    campground_id: Mapped[str | None] = mapped_column(String(20))
    campsite_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    sites: Mapped[list[str]] = mapped_column(JSON, default=list)
    # The alert's own date range.
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    # "As many as 796 other individuals may be receiving this notification."
    other_recipients: Mapped[int | None]
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # The email's check found something new (the email beat the watcher).
    triggered_finds: Mapped[int] = mapped_column(Integer, default=0)
    opening_id: Mapped[int | None] = mapped_column(
        ForeignKey("openings.id", ondelete="SET NULL")
    )
    watcher_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    outcome: Mapped[str | None] = mapped_column(String(20))
    lead_seconds: Mapped[float | None]
