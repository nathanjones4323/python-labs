"""Booking preferences and runtime settings.

Preferences (which day, which time window) live here as constants; anything
secret or environment-specific comes from the environment / `.env`.
"""

import os
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Course --------------------------------------------------------------------
COURSE_NAME = "Soule Park"
COURSE_TZ = ZoneInfo("America/Los_Angeles")

# foreUP identifiers for Soule Park's public booking page.
FOREUP_COURSE_ID = "19620"
BOOKING_CLASS = "4718"
SCHEDULE_ID = "1945"

BOOKING_URL = f"https://foreupsoftware.com/index.php/booking/index/{FOREUP_COURSE_ID}"
TEE_TIMES_API_URL = "https://foreupsoftware.com/index.php/api/booking/times"

# --- Tee time preferences -------------------------------------------------------
# Book the Nth upcoming occurrence of TARGET_WEEKDAY (Monday=0 ... Sunday=6).
# "Upcoming" includes today if today is that weekday.
TARGET_WEEKDAY = 5  # Saturday
WEEKS_AHEAD = 3  # the 3rd upcoming Saturday

# Acceptable tee-off window (course-local time), inclusive on both ends.
EARLIEST_TEE_TIME = time(8, 0)
LATEST_TEE_TIME = time(14, 0)
# Within the window, take the earliest slot. False takes the latest.
PREFER_EARLIER = True

HOLES = 18
PLAYERS = 4

# Date format foreUP uses in both the API query string and the UI date field.
FOREUP_DATE_FORMAT = "%m-%d-%Y"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    card_number: str
    card_expiration_month: str
    card_expiration_year: str
    card_cvc: str
    card_postal_code: str

    @classmethod
    def from_env(cls) -> "Credentials":
        def req(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"Missing required environment variable {name}")
            return value

        return cls(
            email=req("SOULE_EMAIL"),
            password=req("SOULE_PASSWORD"),
            card_number=req("CREDIT_CARD_NUMBER"),
            card_expiration_month=req("CREDIT_CARD_EXPIRATION_MONTH"),
            card_expiration_year=req("CREDIT_CARD_EXPIRATION_YEAR"),
            card_cvc=req("CREDIT_CARD_CVC"),
            card_postal_code=req("CREDIT_CARD_POSTAL_CODE"),
        )


@dataclass(frozen=True)
class Settings:
    submit_payment: bool
    headless: bool
    log_path: Path
    artifacts_dir: Path
    # Testing aid: book this date (YYYY-MM-DD) instead of the computed one.
    target_date_override: date | None

    @classmethod
    def from_env(cls) -> "Settings":
        raw_date = os.environ.get("TEE_TIME_TARGET_DATE", "").strip()
        return cls(
            submit_payment=_env_flag("TEE_TIME_SUBMIT_PAYMENT", default=False),
            headless=_env_flag("TEE_TIME_HEADLESS", default=True),
            log_path=Path(os.environ.get("TEE_TIME_LOG_PATH", "log.csv")),
            artifacts_dir=Path(os.environ.get("TEE_TIME_ARTIFACTS_DIR", "artifacts")),
            target_date_override=date.fromisoformat(raw_date) if raw_date else None,
        )
