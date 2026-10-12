"""Initial schema: couples, members, venues, dates, stops, travel cache.

Revision ID: 001
Revises:
Create Date: 2026-10-11 00:00:00.000000

The earlier iterations of this schema were folded into this one revision.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


booking_channel = _enum(
    "booking_channel",
    "resy",
    "opentable",
    "tock",
    "sevenrooms",
    "phone",
    "walk_in",
    "unknown",
    "ticketed",
)
window_unit = _enum("window_unit", "days", "months")
date_status = _enum(
    "date_status", "planning", "booking_pending", "confirmed", "completed", "cancelled"
)
venue_type = _enum(
    "venue_type", "restaurant", "trail", "museum", "bar", "park", "viewpoint", "other"
)
stop_kind = _enum("stop_kind", "activity", "meal", "ticketed")
stop_anchor = _enum("stop_anchor", "none", "fixed", "sunset")
reservation_need = _enum(
    "reservation_need", "required", "recommended", "none", "unknown"
)
stop_booking_status = _enum(
    "stop_booking_status",
    "not_needed",
    "needed",
    "link_sent",
    "reminder_set",
    "confirmed",
    "cancelled",
)
travel_mode = _enum("travel_mode", "driving", "walking")
ENUMS = (
    booking_channel,
    window_unit,
    date_status,
    venue_type,
    stop_kind,
    stop_anchor,
    reservation_need,
    stop_booking_status,
    travel_mode,
)


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "couples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("shared_calendar_id", sa.String(length=320), nullable=True),
        sa.Column("home_area", sa.String(length=200), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("budget_per_date", sa.Integer(), nullable=True),
        sa.Column("max_travel_minutes", sa.SmallInteger(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "couple_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("couple_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("likes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("dislikes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("dietary", postgresql.ARRAY(sa.Text()), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["couple_id"], ["couples.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # A name is how the user refers to a member, so it's unique per couple.
    # Leading column is couple_id, so this also serves lookups by couple.
    op.create_index(
        "ix_couple_members_couple_name",
        "couple_members",
        ["couple_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "venues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("venue_type", venue_type, nullable=False),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("default_duration_min", sa.Integer(), nullable=True),
        sa.Column("booking_channel", booking_channel, nullable=False),
        sa.Column("platform_slug", sa.String(length=200), nullable=True),
        sa.Column("platform_city_slug", sa.String(length=120), nullable=True),
        sa.Column("google_place_id", sa.Text(), nullable=True),
        sa.Column("google_place_data", postgresql.JSONB(), nullable=True),
        sa.Column("window_amount", sa.SmallInteger(), nullable=True),
        sa.Column("window_unit", window_unit, nullable=True),
        sa.Column("window_open_time", sa.Time(), nullable=True),
        sa.Column("channel_evidence", sa.Text(), nullable=True),
        sa.Column("channel_confidence", sa.Float(), nullable=True),
        sa.Column("channel_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    # One row per venue per city, however it was capitalised on the way in.
    op.create_index(
        "ix_venues_name_city",
        "venues",
        [sa.text("lower(name)"), sa.text("lower(city)")],
        unique=True,
    )

    op.create_table(
        "dates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("couple_id", sa.Uuid(), nullable=False),
        # Null: planned for both. Set when the date is one person's pick.
        sa.Column("planned_for_member_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("max_leg_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("start_after", sa.Time(), nullable=True),
        sa.Column("end_before", sa.Time(), nullable=True),
        sa.Column("status", date_status, nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("preview_url", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["couple_id"], ["couples.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["planned_for_member_id"], ["couple_members.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dates_couple_id", "dates", ["couple_id"])
    op.create_index("ix_dates_status", "dates", ["status"])

    op.create_table(
        "date_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("date_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("kind", stop_kind, nullable=False),
        sa.Column("venue_id", sa.Uuid(), nullable=True),
        sa.Column("party_size", sa.SmallInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("est_duration_min", sa.Integer(), nullable=True),
        sa.Column("anchor", stop_anchor, nullable=False),
        sa.Column("sunset_offset_min", sa.SmallInteger(), nullable=False),
        sa.Column("reservation_need", reservation_need, nullable=False),
        sa.Column("booking_status", stop_booking_status, nullable=False),
        sa.Column("booking_url", sa.Text(), nullable=True),
        sa.Column("confirmation_number", sa.String(length=120), nullable=True),
        sa.Column("calendar_event_id", sa.String(length=300), nullable=True),
        sa.Column("reminder_event_id", sa.String(length=300), nullable=True),
        sa.Column("travel_min_from_prev", sa.SmallInteger(), nullable=True),
        sa.Column("travel_mode", travel_mode, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["date_id"], ["dates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["venue_id"], ["venues.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "date_id",
            "position",
            name="uq_date_stops_date_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_date_stops_date_id", "date_stops", ["date_id"])

    op.create_table(
        "travel_cache",
        sa.Column("from_venue_id", sa.Uuid(), nullable=False),
        sa.Column("to_venue_id", sa.Uuid(), nullable=False),
        sa.Column("mode", travel_mode, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("minutes", sa.Float(), nullable=False),
        sa.Column("meters", sa.Float(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["from_venue_id"], ["venues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_venue_id"], ["venues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("from_venue_id", "to_venue_id", "mode", "provider"),
    )


def downgrade() -> None:
    op.drop_table("travel_cache")
    op.drop_table("date_stops")
    op.drop_table("dates")
    op.drop_table("venues")
    op.drop_table("couple_members")
    op.drop_table("couples")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
