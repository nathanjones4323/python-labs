"""Course location, for searching the courses near home.

GolfNow courses already kept their coordinates in `platform_config`; they
move to the new columns, so `CourseConfig` gets them once.

Revision ID: 005
Revises: 004
Create Date: 2026-10-22 19:33:02.694735

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COORDINATES = ("latitude", "longitude")

courses = sa.table(
    "courses",
    sa.column("id"),
    sa.column("platform", sa.String()),
    sa.column("latitude", sa.Float()),
    sa.column("longitude", sa.Float()),
    sa.column(
        "platform_config", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    ),
)


def upgrade() -> None:
    op.add_column("courses", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("courses", sa.Column("longitude", sa.Float(), nullable=True))
    bind = op.get_bind()
    for id_, config in bind.execute(sa.select(courses.c.id, courses.c.platform_config)):
        if not all(key in (config or {}) for key in COORDINATES):
            continue
        bind.execute(
            courses.update()
            .where(courses.c.id == id_)
            .values(
                latitude=config["latitude"],
                longitude=config["longitude"],
                platform_config={
                    k: v for k, v in config.items() if k not in COORDINATES
                },
            )
        )


def downgrade() -> None:
    # Only GolfNow's config took coordinates before; anyone else's are dropped.
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            courses.c.id,
            courses.c.platform_config,
            courses.c.latitude,
            courses.c.longitude,
        ).where(courses.c.platform == "golfnow")
    ).all()
    for id_, config, latitude, longitude in rows:
        bind.execute(
            courses.update()
            .where(courses.c.id == id_)
            .values(
                platform_config={
                    **(config or {}),
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
        )
    op.drop_column("courses", "longitude")
    op.drop_column("courses", "latitude")
