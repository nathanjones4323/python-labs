"""add full-text search index on foods.description

Revision ID: 003
Revises: 002
Create Date: 2026-04-19
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE foods
        ADD COLUMN IF NOT EXISTS description_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', description)) STORED
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_foods_description_fts
        ON foods USING GIN (description_tsv)
    """)
    # Plain lower-case index for prefix / LIKE searches as a fallback
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_foods_description_lower
        ON foods (lower(description))
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_foods_description_fts")
    op.execute("DROP INDEX IF EXISTS idx_foods_description_lower")
    op.execute("ALTER TABLE foods DROP COLUMN IF EXISTS description_tsv")
