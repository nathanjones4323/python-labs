"""Runtime settings, from the environment / `.env`.

What's been billed, paid and requested lives in the database
(`pay_my_bills.db`); who the roommate is and where the secrets are comes from
here. Every variable is prefixed or app-specific so a `.env` further up the
repo (the meal planner's `DATABASE_URL`) can't leak in.
"""

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

CONFIG_DIR = Path("~/.config/pay-my-bills").expanduser()
DATA_DIR = Path("~/.local/share/pay-my-bills").expanduser()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    raw = _optional(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, not {raw!r}") from None


def database_url() -> str:
    return _optional("PAY_MY_BILLS_DATABASE_URL", f"sqlite:///{DATA_DIR / 'bills.db'}")


@dataclass(frozen=True)
class GmailSettings:
    # The Desktop OAuth client downloaded from Google Cloud (see README).
    client_secrets: Path
    # Written by `pay-my-bills-login`, refreshed in place after that.
    token_file: Path

    @classmethod
    def from_env(cls) -> "GmailSettings":
        return cls(
            client_secrets=Path(
                _optional(
                    "GMAIL_CLIENT_SECRETS", str(CONFIG_DIR / "client_secret.json")
                )
            ).expanduser(),
            token_file=Path(
                _optional("GMAIL_TOKEN_FILE", str(CONFIG_DIR / "token.json"))
            ).expanduser(),
        )


@dataclass(frozen=True)
class Settings:
    database_url: str
    # The roommate as Venmo shows them: the display name on "X paid your
    # request" emails, and the username a request goes to.
    roommate_name: str
    roommate_venmo: str
    # Shown as a fallback line when set; Zelle has no link to prefill.
    roommate_zelle: str | None
    # The roommate's share of every bill, in percent.
    split_percent: int
    # SCE autopay debits this many days before the due date. SCE doesn't
    # email when it does, so that's when the request goes out.
    electric_autopay_days_before_due: int
    # Bill dates are local dates.
    timezone: ZoneInfo
    # The whole month's rent, charged (split_percent of it) on the last day
    # of each month for the month after. None: rent isn't handled.
    rent_total_cents: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        split = _int("SPLIT_PERCENT", 50)
        if not 0 < split < 100:
            raise RuntimeError("SPLIT_PERCENT must be between 1 and 99")
        rent = os.environ.get("RENT_TOTAL", "").strip().replace(",", "")
        try:
            rent_cents = round(Decimal(rent) * 100) if rent else None
        except InvalidOperation:
            raise RuntimeError(f"RENT_TOTAL must be dollars, not {rent!r}") from None
        return cls(
            database_url=database_url(),
            roommate_name=_required("ROOMMATE_NAME"),
            roommate_venmo=_required("ROOMMATE_VENMO").removeprefix("@"),
            roommate_zelle=os.environ.get("ROOMMATE_ZELLE", "").strip() or None,
            split_percent=split,
            electric_autopay_days_before_due=_int("SCE_AUTOPAY_DAYS_BEFORE_DUE", 11),
            timezone=ZoneInfo(
                _optional("PAY_MY_BILLS_TIMEZONE", "America/Los_Angeles")
            ),
            rent_total_cents=rent_cents,
        )


@dataclass(frozen=True)
class BotSettings:
    """What the Telegram bot needs on top of `Settings`."""

    telegram_token: str
    # Only these Telegram users are answered, and every message goes to all
    # of them (a user's private chat id is their user id).
    allowed_user_ids: frozenset[int]
    poll_seconds: int

    @classmethod
    def from_env(cls) -> "BotSettings":
        raw_ids = _required("TELEGRAM_ALLOWED_USER_IDS")
        try:
            ids = frozenset(int(part) for part in raw_ids.split(",") if part.strip())
        except ValueError:
            raise RuntimeError(
                "TELEGRAM_ALLOWED_USER_IDS must be numeric Telegram user ids, "
                "comma-separated (ask @userinfobot for yours)"
            ) from None
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=ids,
            poll_seconds=max(_int("GMAIL_POLL_SECONDS", 120), 30),
        )
