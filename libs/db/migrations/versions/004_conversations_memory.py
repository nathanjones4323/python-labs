"""add memory fields to conversations

Revision ID: 004
Revises: 003
Create Date: 2026-04-19
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # title and created_at are already used by chat.py but not tracked in migrations
    op.execute("""
        ALTER TABLE conversations
        ADD COLUMN IF NOT EXISTS title TEXT,
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
        ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS extracted BOOLEAN NOT NULL DEFAULT false
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversations_unextracted
        ON conversations(ended_at) WHERE extracted = false
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_conversations_unextracted")
    op.execute("""
        ALTER TABLE conversations
        DROP COLUMN IF EXISTS extracted,
        DROP COLUMN IF EXISTS ended_at,
        DROP COLUMN IF EXISTS created_at,
        DROP COLUMN IF EXISTS title
    """)
