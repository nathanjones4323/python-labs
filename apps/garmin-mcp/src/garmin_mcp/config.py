"""Runtime settings, read once from the environment / `.env`.

Nothing secret lives here: the Garmin password is only ever typed into
`garmin-mcp-login`, and the session tokens it produces stay in the token
store, which the library writes 0600 inside a 0700 directory.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GARMIN_",
        env_file=(".env", ".env.local"),
        extra="ignore",
    )

    # Only used by `garmin-mcp-login` to prefill the prompt.
    email: str | None = None
    # A directory; the library keeps `garmin_tokens.json` in it.
    tokenstore: Path = Path("~/.config/garmin-mcp/tokens")
    # On by default: every write tool previews and stops. Set
    # GARMIN_DRY_RUN=false once the previews look right.
    dry_run: bool = True
    # How long a preview's confirmation token stays valid.
    approval_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    # Used by `push_workout_to_device` when no device is named; otherwise
    # Garmin's last-used device.
    default_device_id: int | None = None

    # The server log and the operation log (`operations.jsonl`).
    state_dir: Path = Path("~/.local/state/garmin-mcp")
    log_level: str = "INFO"

    # Reads that hit a 5xx or a dropped connection are retried this many
    # times by the library, with jittered backoff of 1–10 s. Writes are never
    # retried. (Each request has the library's fixed 15 s timeout.)
    read_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("tokenstore", "state_dir")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def token_file(self) -> Path:
        return self.tokenstore / "garmin_tokens.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
