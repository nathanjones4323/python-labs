"""add google auth columns to users

Revision ID: 002
Revises: 001
Create Date: 2026-04-18
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT UNIQUE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS display_name")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS google_id")
