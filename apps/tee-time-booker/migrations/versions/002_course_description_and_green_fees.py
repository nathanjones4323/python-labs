"""Course description and green-fee estimate.

Revision ID: 002
Revises: 001
Create Date: 2026-09-17 21:33:36.661827

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "courses",
        sa.Column("green_fees", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("courses", "green_fees")
    op.drop_column("courses", "description")
