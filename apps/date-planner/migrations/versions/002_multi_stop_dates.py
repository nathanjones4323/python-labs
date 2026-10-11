"""Multi-stop dates: restaurants -> venues, a date's bookings move to stops.

Revision ID: 002
Revises: 001
Create Date: 2026-10-11 00:00:00.000000

Upgrade turns each v1 date into a container with one stop at position 0,
carrying the restaurant, times, party size and booking fields. The status
moves across like this:

    v1 date            stop booking_status                  v2 date
    proposed/tentative needed (not_needed for a walk-in     planning
                       or a date with no restaurant)
    booking_pending    reminder_set if a reminder id is     booking_pending
                       set, else link_sent
    confirmed          confirmed                            confirmed
    completed          confirmed                            completed
    cancelled          cancelled                            cancelled

Downgrade reads the first stop back onto the date. v1 can hold only one
venue and one time per date, so later stops are dropped; a date that never
had a time gets noon on its day. `proposed` and `tentative` both became
`planning`, and come back apart by whether the stop has a calendar event —
the thing that made a v1 date tentative. Venues the v1 schema can't type
stay as restaurants, and `ticketed` venues go back to `unknown`.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


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
NEW_ENUMS = (
    venue_type,
    stop_kind,
    stop_anchor,
    reservation_need,
    stop_booking_status,
    travel_mode,
)

V1_STATUSES = (
    "proposed",
    "tentative",
    "booking_pending",
    "confirmed",
    "completed",
    "cancelled",
)
V2_STATUSES = ("planning", "booking_pending", "confirmed", "completed", "cancelled")
V1_CHANNELS = (
    "resy",
    "opentable",
    "tock",
    "sevenrooms",
    "phone",
    "walk_in",
    "unknown",
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


def _swap_enum(
    table: str, column: str, name: str, values: Sequence[str], using: str
) -> None:
    """Replace enum type `name` with one holding `values`.

    `using` is the SQL expression (over `{column}::text`) that maps each old
    value to a new one.
    """
    op.execute(f"ALTER TYPE {name} RENAME TO {name}_old")
    postgresql.ENUM(*values, name=name).create(op.get_bind())
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {name} USING ({using})::{name}"
    )
    op.execute(f"DROP TYPE {name}_old")


def upgrade() -> None:
    bind = op.get_bind()

    # --- restaurants -> venues ---------------------------------------------
    op.rename_table("restaurants", "venues")
    op.execute("ALTER INDEX ix_restaurants_name_city RENAME TO ix_venues_name_city")
    op.execute("ALTER INDEX restaurants_pkey RENAME TO venues_pkey")
    for enum in NEW_ENUMS:
        enum.create(bind, checkfirst=True)
    # Backfilled through the server default, which is then dropped: a new
    # venue has to say what it is.
    op.add_column(
        "venues",
        sa.Column(
            "venue_type", venue_type, nullable=False, server_default="restaurant"
        ),
    )
    op.alter_column("venues", "venue_type", server_default=None)
    op.add_column("venues", sa.Column("lat", sa.Float(), nullable=True))
    op.add_column("venues", sa.Column("lng", sa.Float(), nullable=True))
    op.add_column("venues", sa.Column("external_url", sa.Text(), nullable=True))
    op.add_column(
        "venues", sa.Column("default_duration_min", sa.Integer(), nullable=True)
    )
    # Not used in this transaction, so adding it here is safe on PG 12+.
    op.execute("ALTER TYPE booking_channel ADD VALUE IF NOT EXISTS 'ticketed'")

    # --- dates become containers -------------------------------------------
    op.add_column("dates", sa.Column("day", sa.Date(), nullable=True))
    op.add_column(
        "dates",
        sa.Column(
            "max_leg_minutes", sa.SmallInteger(), nullable=False, server_default="25"
        ),
    )
    op.alter_column("dates", "max_leg_minutes", server_default=None)
    op.add_column("dates", sa.Column("start_after", sa.Time(), nullable=True))
    op.add_column("dates", sa.Column("end_before", sa.Time(), nullable=True))
    op.execute(
        """
        UPDATE dates d SET day = (d.start_at AT TIME ZONE c.timezone)::date
        FROM couples c WHERE c.id = d.couple_id
        """
    )
    op.alter_column("dates", "day", nullable=False)

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

    # One stop per v1 date. The stop keeps the date's own timestamps, so a
    # v1 date's history reads the same afterwards. A time with a venue that
    # takes bookings was the time it was booked for, so it's fixed.
    op.execute(
        """
        INSERT INTO date_stops (
            id, date_id, position, title, kind, venue_id, party_size,
            start_at, end_at, est_duration_min, anchor, sunset_offset_min,
            reservation_need, booking_status, booking_url, confirmation_number,
            calendar_event_id, reminder_event_id, travel_mode, notes,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(), d.id, 0, d.title,
            (CASE WHEN d.restaurant_id IS NULL
                  THEN 'activity' ELSE 'meal' END)::stop_kind,
            d.restaurant_id, d.party_size, d.start_at, d.end_at,
            (EXTRACT(EPOCH FROM d.end_at - d.start_at) / 60)::int,
            (CASE WHEN v.booking_channel IS NOT NULL
                       AND v.booking_channel::text <> 'walk_in'
                  THEN 'fixed' ELSE 'none' END)::stop_anchor,
            -30,
            (CASE
                WHEN v.booking_channel IS NULL THEN 'unknown'
                WHEN v.booking_channel::text = 'walk_in' THEN 'none'
                WHEN v.booking_channel::text = 'unknown' THEN 'unknown'
                ELSE 'recommended'
             END)::reservation_need,
            (CASE d.status::text
                WHEN 'booking_pending' THEN
                    CASE WHEN d.reminder_event_id IS NOT NULL
                         THEN 'reminder_set' ELSE 'link_sent' END
                WHEN 'confirmed' THEN 'confirmed'
                WHEN 'completed' THEN 'confirmed'
                WHEN 'cancelled' THEN 'cancelled'
                ELSE
                    CASE WHEN v.booking_channel IS NULL
                              OR v.booking_channel::text = 'walk_in'
                         THEN 'not_needed' ELSE 'needed' END
             END)::stop_booking_status,
            d.booking_url, d.confirmation_number,
            d.calendar_event_id, d.reminder_event_id,
            'driving'::travel_mode, NULL,
            d.created_at, d.updated_at
        FROM dates d LEFT JOIN venues v ON v.id = d.restaurant_id
        """
    )

    _swap_enum(
        "dates",
        "status",
        "date_status",
        V2_STATUSES,
        """CASE status::text
            WHEN 'proposed' THEN 'planning'
            WHEN 'tentative' THEN 'planning'
            ELSE status::text
        END""",
    )

    op.drop_constraint("dates_restaurant_id_fkey", "dates", type_="foreignkey")
    for column in (
        "restaurant_id",
        "start_at",
        "end_at",
        "party_size",
        "calendar_event_id",
        "reminder_event_id",
        "booking_url",
        "confirmation_number",
    ):
        op.drop_column("dates", column)


def downgrade() -> None:
    bind = op.get_bind()

    # --- stops back onto the date ------------------------------------------
    op.add_column("dates", sa.Column("restaurant_id", sa.Uuid(), nullable=True))
    op.add_column("dates", sa.Column("start_at", sa.DateTime(timezone=True)))
    op.add_column("dates", sa.Column("end_at", sa.DateTime(timezone=True)))
    op.add_column("dates", sa.Column("party_size", sa.SmallInteger()))
    op.add_column("dates", sa.Column("calendar_event_id", sa.String(length=300)))
    op.add_column("dates", sa.Column("reminder_event_id", sa.String(length=300)))
    op.add_column("dates", sa.Column("booking_url", sa.Text()))
    op.add_column("dates", sa.Column("confirmation_number", sa.String(length=120)))
    op.execute(
        """
        UPDATE dates d SET
            restaurant_id = s.venue_id,
            start_at = s.start_at,
            end_at = s.end_at,
            party_size = s.party_size,
            calendar_event_id = s.calendar_event_id,
            reminder_event_id = s.reminder_event_id,
            booking_url = s.booking_url,
            confirmation_number = s.confirmation_number
        FROM (
            SELECT DISTINCT ON (date_id) * FROM date_stops
            ORDER BY date_id, position
        ) s
        WHERE s.date_id = d.id
        """
    )
    # Dates v1 couldn't have held: no stop, or a stop with no time yet.
    op.execute(
        """
        UPDATE dates d SET
            start_at = COALESCE(
                d.start_at,
                (d.day + COALESCE(d.start_after, time '12:00'))
                    AT TIME ZONE c.timezone
            ),
            party_size = COALESCE(d.party_size, 2)
        FROM couples c WHERE c.id = d.couple_id
        """
    )
    op.execute(
        """
        UPDATE dates SET end_at = start_at + interval '2 hours'
        WHERE end_at IS NULL OR end_at <= start_at
        """
    )
    for column in ("start_at", "end_at", "party_size"):
        op.alter_column("dates", column, nullable=False)

    _swap_enum(
        "dates",
        "status",
        "date_status",
        V1_STATUSES,
        """CASE status::text
            WHEN 'planning' THEN
                CASE WHEN calendar_event_id IS NULL
                     THEN 'proposed' ELSE 'tentative' END
            ELSE status::text
        END""",
    )

    op.drop_table("travel_cache")
    op.drop_index("ix_date_stops_date_id", table_name="date_stops")
    op.drop_table("date_stops")
    for column in ("end_before", "start_after", "max_leg_minutes", "day"):
        op.drop_column("dates", column)

    # --- venues -> restaurants ---------------------------------------------
    for column in ("default_duration_min", "external_url", "lng", "lat", "venue_type"):
        op.drop_column("venues", column)
    # Postgres can't drop an enum value; rebuild the type without it.
    _swap_enum(
        "venues",
        "booking_channel",
        "booking_channel",
        V1_CHANNELS,
        """CASE booking_channel::text
            WHEN 'ticketed' THEN 'unknown'
            ELSE booking_channel::text
        END""",
    )
    op.execute("ALTER INDEX venues_pkey RENAME TO restaurants_pkey")
    op.execute("ALTER INDEX ix_venues_name_city RENAME TO ix_restaurants_name_city")
    op.rename_table("venues", "restaurants")
    op.create_foreign_key(
        "dates_restaurant_id_fkey",
        "dates",
        "restaurants",
        ["restaurant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for enum in reversed(NEW_ENUMS):
        enum.drop(bind, checkfirst=True)
