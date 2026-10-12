"""Runtime settings, read once from the environment / `.env`.

Who the couple is and where they like to eat lives in the database; only
environment-specific knobs land here.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Restaurant sites are quick to block an obvious script. This is a plain
# desktop Chrome string; override it with DATE_PLANNER_USER_AGENT.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


CARTO_POSITRON = (
    "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}@2x.png"
)
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


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

    # Travel between stops; see `planning.travel`. Unprefixed, like
    # DATABASE_URL, because they name outside services. The default needs no
    # network and no key.
    travel_provider: Literal["haversine", "osrm", "ors", "google"] = Field(
        default="haversine", alias="TRAVEL_PROVIDER"
    )
    osrm_url: str = Field(default="http://localhost:5000", alias="OSRM_URL")
    ors_api_key: str | None = Field(default=None, alias="ORS_API_KEY")
    # Added to every driving time: neither OSM provider knows about traffic,
    # and PCH on a weekend is not free-flow.
    travel_padding_pct: float = Field(default=20.0, alias="TRAVEL_PADDING_PCT")
    travel_cache_ttl_days: int = 30

    # Google Places (New), for a venue's Place ID — the one thing the Resy
    # connector's widget needs. Unprefixed, like ORS_API_KEY. Without it
    # `find_google_place` says so and the skill falls back to the link.
    google_places_api_key: str | None = Field(
        default=None, alias="GOOGLE_PLACES_API_KEY"
    )

    # The date preview page; see `preview`. Pages are written under
    # `preview_dir` (relative to where the server starts, which is the
    # Claude Code project) so the session can publish them as an Artifact.
    # The basemap is CARTO's Positron when `CARTO_API_KEY` is set (a free
    # key, no account, from carto.com/basemaps/apikey; without one CARTO
    # answers every tile with an "API key required" watermark), otherwise
    # OpenStreetMap's own tiles. Both are free for light use like this (up
    # to 200 tiles per date at zoom 16 and below, cached) with an
    # identifying User-Agent and the credit on the map. Without
    # `map_tiles_dark` the page darkens the light map itself, and the
    # interactive map always does. `map_tiles_light` overrides the choice
    # with any {z}/{x}/{y} template, 256px or @2x (set `map_attribution` to
    # match); an empty one turns the basemap off and leaves the pins on a
    # plain grid.
    preview_dir: Path = Path("previews")
    carto_api_key: str | None = Field(default=None, alias="CARTO_API_KEY")
    map_tiles_light: str | None = None
    map_tiles_dark: str = ""
    map_attribution: str | None = None
    # Tile servers ask for an honest client name, unlike restaurant sites.
    map_user_agent: str = "date-planner/0.1 (personal date preview)"
    map_tile_ttl_days: int = 30

    # Read only by `date_planner.seed`, so a wiped database comes back with
    # the real couple. `seed_member_emails` is comma-separated.
    shared_calendar_id: str | None = None
    seed_member_emails: str = ""

    # The server's log file, `log_dir/date-planner.log`; see `logs`.
    # Relative to where the server starts, like `preview_dir`.
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    # Rotated at `log_max_mb`, with `log_backups` old files kept.
    log_max_mb: int = 20
    log_backups: int = 25

    # `--http` only. Without a token the HTTP transport refuses to start, so
    # a stray `--http` can't expose the database unauthenticated.
    http_token: str | None = None
    http_host: str = "127.0.0.1"
    http_port: int = 8002

    def light_tiles(self) -> str:
        """The light basemap's tile template; empty for no basemap."""
        if self.map_tiles_light is not None:
            return self.map_tiles_light
        if self.carto_api_key:
            return CARTO_POSITRON + "?key=" + quote(self.carto_api_key, safe="")
        return OSM_TILES

    def map_credit(self) -> str:
        if self.map_attribution is not None:
            return self.map_attribution
        if self.map_tiles_light is None and self.carto_api_key:
            return "© OpenStreetMap contributors © CARTO"
        return "© OpenStreetMap contributors"

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError("Missing required environment variable DATABASE_URL")
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
