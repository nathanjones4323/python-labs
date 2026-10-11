"""Runtime settings, read once from the environment / `.env`.

Who the couple is and where they like to eat lives in the database; only
environment-specific knobs land here.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Restaurant sites are quick to block an obvious script. This is a plain
# desktop Chrome string; override it with DATE_PLANNER_USER_AGENT.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATE_PLANNER_",
        env_file=".env",
        extra="ignore",
    )

    # Not prefixed: the whole repo uses DATABASE_URL. None works for the
    # tools that don't touch the database; `require_database_url` names the
    # variable otherwise.
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    user_agent: str = DEFAULT_USER_AGENT
    fetch_timeout: float = 10.0
    # Homepage plus the reservation pages worth trying; see booking.detect.
    max_pages: int = 4
    # A cached booking channel older than this is looked up again.
    detection_ttl_days: int = 30

    # `--http` only. Without a token the HTTP transport refuses to start, so
    # a stray `--http` can't expose the database unauthenticated.
    http_token: str | None = None
    http_host: str = "127.0.0.1"
    http_port: int = 8002

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError("Missing required environment variable DATABASE_URL")
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
