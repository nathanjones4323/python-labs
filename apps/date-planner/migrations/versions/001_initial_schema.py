"""Initial schema: couples, restaurants, dates.

Revision ID: 001
Revises:
Create Date: 2026-10-10 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

booking_channel = sa.Enum(
    "resy",
    "opentable",
    "tock",
    "sevenrooms",
    "phone",
    "walk_in",
    "unknown",
    name="booking_channel",
)
window_unit = sa.Enum("days", "months", name="window_unit")
date_status = sa.Enum(
    "proposed",
    "tentative",
    "booking_pending",
    "confirmed",
    "completed",
    "cancelled",
    name="date_status",
)


def upgrade() -> None:
    op.create_table(
        "couples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("member_emails", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("shared_calendar_id", sa.String(length=320), nullable=True),
        sa.Column("home_area", sa.String(length=200), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("budget_per_date", sa.Integer(), nullable=True),
        sa.Column("dietary", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("likes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("dislikes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("max_travel_minutes", sa.SmallInteger(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "restaurants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("address", sa.String(length=300), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("booking_channel", booking_channel, nullable=False),
        sa.Column("platform_slug", sa.String(length=200), nullable=True),
        sa.Column("platform_city_slug", sa.String(length=120), nullable=True),
        sa.Column("window_amount", sa.SmallInteger(), nullable=True),
        sa.Column("window_unit", window_unit, nullable=True),
        sa.Column("window_open_time", sa.Time(), nullable=True),
        sa.Column("channel_evidence", sa.Text(), nullable=True),
        sa.Column("channel_confidence", sa.Float(), nullable=True),
        sa.Column("channel_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    # One row per venue per city, however it was capitalised on the way in.
    op.create_index(
        "ix_restaurants_name_city",
        "restaurants",
        [sa.text("lower(name)"), sa.text("lower(city)")],
        unique=True,
    )
    op.create_table(
        "dates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("couple_id", sa.Uuid(), nullable=False),
        sa.Column("restaurant_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("party_size", sa.SmallInteger(), nullable=False),
        sa.Column("status", date_status, nullable=False),
        sa.Column("calendar_event_id", sa.String(length=300), nullable=True),
        sa.Column("reminder_event_id", sa.String(length=300), nullable=True),
        sa.Column("booking_url", sa.Text(), nullable=True),
        sa.Column("confirmation_number", sa.String(length=120), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["couple_id"], ["couples.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["restaurant_id"], ["restaurants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dates_couple_id", "dates", ["couple_id"])
    op.create_index("ix_dates_status", "dates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_dates_status", table_name="dates")
    op.drop_index("ix_dates_couple_id", table_name="dates")
    op.drop_table("dates")
    op.drop_index("ix_restaurants_name_city", table_name="restaurants")
    op.drop_table("restaurants")
    op.drop_table("couples")
    bind = op.get_bind()
    date_status.drop(bind, checkfirst=True)
    window_unit.drop(bind, checkfirst=True)
    booking_channel.drop(bind, checkfirst=True)
