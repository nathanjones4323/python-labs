"""One venue per Google place.

Revision ID: 002
Revises: 001
Create Date: 2026-10-11 21:00:00.000000

A venue is found by a fuzzy name, so a differently-worded name could still
slip through as a second row. Once Google names the place, this stops two
rows holding the same one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_venues_google_place_id",
        "venues",
        ["google_place_id"],
        unique=True,
        postgresql_where=sa.text("google_place_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_venues_google_place_id", table_name="venues")
