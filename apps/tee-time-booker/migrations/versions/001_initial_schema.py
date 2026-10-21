"""Initial schema: users, courses, site accounts, plans, requests, runs.

Revision ID: 001
Revises:
Create Date: 2026-09-17 20:23:42.678091

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("release_opens_at", sa.Time(), nullable=True),
        sa.Column("release_pre_start", sa.Time(), nullable=True),
        sa.Column("booking_window_days", sa.SmallInteger(), nullable=True),
        sa.Column(
            "platform_config",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "site_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=50), nullable=False),
        sa.Column("login_email", sa.String(length=320), nullable=False),
        sa.Column("env_prefix", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "env_prefix"),
    )
    op.create_index(
        op.f("ix_site_accounts_user_id"), "site_accounts", ["user_id"], unique=False
    )
    op.create_table(
        "booking_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("site_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("target_weekday", sa.SmallInteger(), nullable=False),
        sa.Column("weeks_ahead", sa.SmallInteger(), nullable=False),
        sa.Column("earliest", sa.Time(), nullable=False),
        sa.Column("latest", sa.Time(), nullable=False),
        sa.Column("holes", sa.SmallInteger(), nullable=False),
        sa.Column("players", sa.SmallInteger(), nullable=False),
        sa.Column("prefer_earlier", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
        ),
        sa.ForeignKeyConstraint(
            ["site_account_id"],
            ["site_accounts.id"],
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_booking_plans_user_id"), "booking_plans", ["user_id"], unique=False
    )
    op.create_table(
        "booking_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("site_account_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("earliest", sa.Time(), nullable=False),
        sa.Column("latest", sa.Time(), nullable=False),
        sa.Column("holes", sa.SmallInteger(), nullable=False),
        sa.Column("players", sa.SmallInteger(), nullable=False),
        sa.Column("prefer_earlier", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "booked",
                "failed",
                "unknown",
                "cancelled",
                name="request_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["booking_plans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["site_account_id"],
            ["site_accounts.id"],
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "target_date"),
    )
    op.create_index(
        op.f("ix_booking_requests_status"), "booking_requests", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_booking_requests_user_id"),
        "booking_requests",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running", "success", "dry_run", "error", "unknown", name="run_status"
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("tee_time_local", sa.DateTime(), nullable=True),
        sa.Column("side", sa.String(length=20), nullable=True),
        sa.Column("green_fee", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("service_fee", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("confirmation", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["booking_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_runs_request_id"), "runs", ["request_id"], unique=False)
    op.create_index(op.f("ix_runs_started_at"), "runs", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_started_at"), table_name="runs")
    op.drop_index(op.f("ix_runs_request_id"), table_name="runs")
    op.drop_table("runs")
    op.drop_index(op.f("ix_booking_requests_user_id"), table_name="booking_requests")
    op.drop_index(op.f("ix_booking_requests_status"), table_name="booking_requests")
    op.drop_table("booking_requests")
    op.drop_index(op.f("ix_booking_plans_user_id"), table_name="booking_plans")
    op.drop_table("booking_plans")
    op.drop_index(op.f("ix_site_accounts_user_id"), table_name="site_accounts")
    op.drop_table("site_accounts")
    op.drop_table("users")
    op.drop_table("courses")
    # Postgres keeps the enums as types of their own; SQLite has none to drop.
    if op.get_bind().dialect.name == "postgresql":
        sa.Enum(name="run_status").drop(op.get_bind())
        sa.Enum(name="request_status").drop(op.get_bind())
