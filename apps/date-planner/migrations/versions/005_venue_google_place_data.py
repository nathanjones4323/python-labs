"""The Google Places response for a venue.

Revision ID: 005
Revises: 004
Create Date: 2026-10-11 19:00:00.000000

`find_google_place` keeps the place exactly as Places returned it, so
nothing Google sent has to be fetched again.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("venues", sa.Column("google_place_data", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("venues", "google_place_data")
