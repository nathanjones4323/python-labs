"""meal planner tables

Revision ID: 001
Revises:
Create Date: 2026-04-15
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            dietary_restrictions TEXT[] DEFAULT '{}',
            allergies TEXT[] DEFAULT '{}',
            goals TEXT,
            calorie_target INT,
            meals_per_day INT DEFAULT 3,
            preferences JSONB DEFAULT '{}',
            onboarding_complete BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            messages JSONB NOT NULL DEFAULT '[]',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS foods (
            fdc_id INT PRIMARY KEY,
            description TEXT NOT NULL,
            food_category TEXT,
            data_type TEXT,
            nutrients JSONB NOT NULL DEFAULT '{}',
            cached_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS meal_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            meal_plan_id UUID NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'snack')),
            scheduled_date DATE NOT NULL,
            instructions TEXT[] DEFAULT '{}',
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS meal_foods (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            meal_id UUID NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            fdc_id INT NOT NULL REFERENCES foods(fdc_id),
            quantity_g DECIMAL(10, 2) NOT NULL,
            serving_description TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS idx_meal_plans_user_id ON meal_plans(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_meals_meal_plan_id ON meals(meal_plan_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_meals_scheduled_date ON meals(scheduled_date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_meal_foods_meal_id ON meal_foods(meal_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meal_foods")
    op.execute("DROP TABLE IF EXISTS meals")
    op.execute("DROP TABLE IF EXISTS meal_plans")
    op.execute("DROP TABLE IF EXISTS foods")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS user_profiles")
    op.execute("DROP TABLE IF EXISTS users")
