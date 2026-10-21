"""Scheduled booking: auto-book flags, scheduled runs, and a not-open outcome.

Revision ID: 003
Revises: 002
Create Date: 2026-10-21 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "booking_plans",
        sa.Column("auto_book", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "booking_requests",
        sa.Column("auto_book", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "runs",
        sa.Column("scheduled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # SQLite stores the enum as plain text, so only Postgres needs the value.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE run_status ADD VALUE IF NOT EXISTS 'not_open'")


def downgrade() -> None:
    # Postgres can't drop an enum value; 'not_open' stays, unused.
    op.execute("UPDATE runs SET status = 'error' WHERE status = 'not_open'")
    op.drop_column("runs", "scheduled")
    op.drop_column("booking_requests", "auto_book")
    op.drop_column("booking_plans", "auto_book")
