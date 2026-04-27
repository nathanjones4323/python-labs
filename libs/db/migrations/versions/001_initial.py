"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-21
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id  TEXT UNIQUE NOT NULL DEFAULT 'user_' || encode(gen_random_bytes(6), 'hex'),
            email        TEXT UNIQUE NOT NULL,
            google_id    TEXT UNIQUE,
            display_name TEXT,
            created_at   TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id               UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            dietary_restrictions  TEXT[] DEFAULT '{}',
            allergies             TEXT[] DEFAULT '{}',
            goals                 TEXT,
            calorie_target        INT,
            meals_per_day         INT DEFAULT 3,
            preferences           JSONB DEFAULT '{}',
            onboarding_complete   BOOLEAN DEFAULT FALSE,
            updated_at            TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id TEXT UNIQUE NOT NULL DEFAULT 'conv_' || encode(gen_random_bytes(6), 'hex'),
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            messages   JSONB NOT NULL DEFAULT '[]',
            title      TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            ended_at   TIMESTAMPTZ,
            extracted  BOOLEAN NOT NULL DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS foods (
            fdc_id          INT PRIMARY KEY,
            description     TEXT NOT NULL,
            food_category   TEXT,
            data_type       TEXT,
            nutrients       JSONB NOT NULL DEFAULT '{}',
            cached_at       TIMESTAMPTZ DEFAULT NOW(),
            description_tsv TSVECTOR GENERATED ALWAYS AS (
                to_tsvector('english', description)
            ) STORED
        );

        CREATE TABLE IF NOT EXISTS meal_plans (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id     TEXT UNIQUE NOT NULL DEFAULT 'mplan_' || encode(gen_random_bytes(6), 'hex'),
            user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name            TEXT NOT NULL,
            start_date      DATE NOT NULL,
            end_date        DATE NOT NULL,
            conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS meals (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id    TEXT UNIQUE NOT NULL DEFAULT 'meal_' || encode(gen_random_bytes(6), 'hex'),
            meal_plan_id   UUID NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            meal_type      TEXT NOT NULL CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
            scheduled_date DATE NOT NULL,
            instructions   TEXT[] DEFAULT '{}',
            notes          TEXT,
            created_at     TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS meal_foods (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id         TEXT UNIQUE NOT NULL DEFAULT 'mfood_' || encode(gen_random_bytes(6), 'hex'),
            meal_id             UUID NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            fdc_id              INT NOT NULL REFERENCES foods(fdc_id),
            quantity_g          DECIMAL(10,2) NOT NULL,
            serving_description TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS favorite_meals (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id    TEXT UNIQUE NOT NULL DEFAULT 'fav_' || encode(gen_random_bytes(6), 'hex'),
            user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            meal_type      TEXT NOT NULL CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
            instructions   TEXT[] DEFAULT '{}',
            notes          TEXT,
            source_meal_id UUID REFERENCES meals(id) ON DELETE SET NULL,
            created_at     TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS favorite_meal_foods (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id         TEXT UNIQUE NOT NULL DEFAULT 'fmfood_' || encode(gen_random_bytes(6), 'hex'),
            favorite_meal_id    UUID NOT NULL REFERENCES favorite_meals(id) ON DELETE CASCADE,
            fdc_id              INT NOT NULL REFERENCES foods(fdc_id),
            quantity_g          DECIMAL(10,2) NOT NULL,
            serving_description TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_meal_plans_user_id
            ON meal_plans(user_id);
        CREATE INDEX IF NOT EXISTS idx_meal_plans_conversation_id
            ON meal_plans(conversation_id) WHERE conversation_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_meals_meal_plan_id
            ON meals(meal_plan_id);
        CREATE INDEX IF NOT EXISTS idx_meals_scheduled_date
            ON meals(scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_meal_foods_meal_id
            ON meal_foods(meal_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_user_id
            ON conversations(user_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_unextracted
            ON conversations(ended_at) WHERE extracted = FALSE;
        CREATE INDEX IF NOT EXISTS idx_foods_description_fts
            ON foods USING GIN (description_tsv);
        CREATE INDEX IF NOT EXISTS idx_foods_description_lower
            ON foods (lower(description));
        CREATE INDEX IF NOT EXISTS idx_favorite_meals_user_id
            ON favorite_meals(user_id);
        CREATE INDEX IF NOT EXISTS idx_favorite_meal_foods_fav_id
            ON favorite_meal_foods(favorite_meal_id);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS idx_favorite_meal_foods_fav_id;
        DROP INDEX IF EXISTS idx_favorite_meals_user_id;
        DROP INDEX IF EXISTS idx_foods_description_lower;
        DROP INDEX IF EXISTS idx_foods_description_fts;
        DROP INDEX IF EXISTS idx_conversations_unextracted;
        DROP INDEX IF EXISTS idx_conversations_user_id;
        DROP INDEX IF EXISTS idx_meal_foods_meal_id;
        DROP INDEX IF EXISTS idx_meals_scheduled_date;
        DROP INDEX IF EXISTS idx_meals_meal_plan_id;
        DROP INDEX IF EXISTS idx_meal_plans_conversation_id;
        DROP INDEX IF EXISTS idx_meal_plans_user_id;

        DROP TABLE IF EXISTS favorite_meal_foods;
        DROP TABLE IF EXISTS favorite_meals;
        DROP TABLE IF EXISTS meal_foods;
        DROP TABLE IF EXISTS meals;
        DROP TABLE IF EXISTS meal_plans;
        DROP TABLE IF EXISTS foods;
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS user_profiles;
        DROP TABLE IF EXISTS users;
    """)
