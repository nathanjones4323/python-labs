"""The date's preview page: where it was published.

Revision ID: 003
Revises: 002
Create Date: 2026-10-11 12:00:00.000000

`render_date_preview` writes a page the session publishes as an Artifact.
Keeping the Artifact's link on the date lets a later conversation republish
to the same link after a change, rather than handing out a new one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("dates", sa.Column("preview_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("dates", "preview_url")
