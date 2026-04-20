"""link meal_plans to the conversation that created them

Revision ID: 005
Revises: 004
Create Date: 2026-04-19
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE meal_plans
        ADD COLUMN IF NOT EXISTS conversation_id UUID
            REFERENCES conversations(id) ON DELETE SET NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_meal_plans_conversation_id
        ON meal_plans(conversation_id)
        WHERE conversation_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_meal_plans_conversation_id")
    op.execute("ALTER TABLE meal_plans DROP COLUMN IF EXISTS conversation_id")
