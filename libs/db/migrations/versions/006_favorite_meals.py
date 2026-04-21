"""add favorite_meals and favorite_meal_foods tables

Revision ID: 006
Revises: 005
Create Date: 2026-04-21
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS favorite_meals (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            meal_type       TEXT NOT NULL CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
            instructions    TEXT[] DEFAULT '{}',
            notes           TEXT,
            source_meal_id  UUID REFERENCES meals(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS favorite_meal_foods (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            favorite_meal_id    UUID NOT NULL REFERENCES favorite_meals(id) ON DELETE CASCADE,
            fdc_id              INT NOT NULL REFERENCES foods(fdc_id),
            quantity_g          DECIMAL(10,2) NOT NULL,
            serving_description TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_favorite_meals_user_id
        ON favorite_meals(user_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_favorite_meal_foods_fav_id
        ON favorite_meal_foods(favorite_meal_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_favorite_meal_foods_fav_id")
    op.execute("DROP INDEX IF EXISTS idx_favorite_meals_user_id")
    op.execute("DROP TABLE IF EXISTS favorite_meal_foods")
    op.execute("DROP TABLE IF EXISTS favorite_meals")
