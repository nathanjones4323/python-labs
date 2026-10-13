"""Ratings per member, 0–10, for a date and for each of its stops.

Revision ID: 003
Revises: 002
Create Date: 2026-10-12 12:00:00.000000

`dates.rating` was one 1–5 score with no owner. It becomes a 0–10 score
(doubled) for every member of the couple, noted as migrated, and the column
goes. Downgrading puts back the average, halved.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

rating_source = postgresql.ENUM(
    "chat", "preview", name="rating_source", create_type=False
)
MIGRATED_NOTE = "Migrated from the 1–5 rating"


def _rating_table(name: str, key: str, parent: str) -> None:
    op.create_table(
        name,
        sa.Column(key, sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", rating_source, nullable=False),
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
        sa.CheckConstraint("score BETWEEN 0 AND 10", name=f"ck_{name}_score"),
        sa.ForeignKeyConstraint([key], [f"{parent}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["couple_members.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(key, "member_id"),
    )


def upgrade() -> None:
    rating_source.create(op.get_bind(), checkfirst=True)
    _rating_table("date_ratings", "date_id", "dates")
    _rating_table("stop_ratings", "stop_id", "date_stops")
    op.execute(
        sa.text(
            """
            INSERT INTO date_ratings (date_id, member_id, score, notes, source)
            SELECT d.id, m.id, d.rating * 2, :note, 'chat'
            FROM dates d
            JOIN couple_members m ON m.couple_id = d.couple_id
            WHERE d.rating IS NOT NULL
            """
        ).bindparams(note=MIGRATED_NOTE)
    )
    op.drop_column("dates", "rating")


def downgrade() -> None:
    op.add_column("dates", sa.Column("rating", sa.SmallInteger(), nullable=True))
    op.execute(
        """
        UPDATE dates d
        SET rating = GREATEST(1, ROUND(r.avg_score / 2.0))
        FROM (
            SELECT date_id, AVG(score) AS avg_score
            FROM date_ratings
            GROUP BY date_id
        ) r
        WHERE r.date_id = d.id
        """
    )
    op.drop_table("stop_ratings")
    op.drop_table("date_ratings")
    rating_source.drop(op.get_bind(), checkfirst=True)
