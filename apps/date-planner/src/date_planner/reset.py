"""Wipe the dev database and seed it again.

    uv run --package date-planner date-planner-reset            # wipe + seed
    uv run --package date-planner date-planner-reset --no-seed  # wipe only

Truncates every table but keeps the schema (and alembic_version), so no
migration is needed afterwards. Refuses a DATABASE_URL that isn't on
localhost; pass --force if you really mean it.
"""

import argparse
import asyncio
from urllib.parse import urlparse

from sqlalchemy import text

from date_planner.config import get_settings
from date_planner.db import open_session
from date_planner.db.models import Base
from date_planner.seed import seed

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


async def wipe() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with open_session() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    print(f"wiped   {tables}")


async def reset(*, run_seed: bool) -> None:
    await wipe()
    if run_seed:
        await seed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-seed", action="store_true", help="wipe only")
    parser.add_argument(
        "--force", action="store_true", help="allow a non-localhost database"
    )
    args = parser.parse_args()

    url = get_settings().require_database_url()
    host = urlparse(url).hostname
    if host not in LOCAL_HOSTS and not args.force:
        raise SystemExit(f"refusing to wipe {host!r}: not localhost (use --force)")
    asyncio.run(reset(run_seed=not args.no_seed))


if __name__ == "__main__":
    main()
