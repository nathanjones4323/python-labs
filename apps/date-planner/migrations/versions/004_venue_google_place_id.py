"""A venue's Google Place ID.

Revision ID: 004
Revises: 003
Create Date: 2026-10-11 18:00:00.000000

The Resy connector's availability widget identifies a restaurant by its
Google Place ID alone. `find_google_place` looks it up once and keeps it
here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("venues", sa.Column("google_place_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("venues", "google_place_id")
