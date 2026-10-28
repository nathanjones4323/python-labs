"""Runtime settings, from the environment / `.env`.

What's watched and what was held lives in the database; logins, the
Telegram bot and where files go come from here. Every variable is prefixed
or app-specific so a `.env` further up the repo can't leak in.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parents[2]
# The app's own .env, whichever directory it's started from (the assistant
# starts the MCP server from apps/assistant).
load_dotenv(APP_DIR / ".env")

DATA_DIR = Path("~/.local/share/campsite-booker").expanduser()
CONFIG_DIR = Path("~/.config/campsite-booker").expanduser()
# pay-my-bills' Desktop OAuth client, used when this app has none of its own.
PAY_MY_BILLS_CLIENT = Path("~/.config/pay-my-bills/client_secret.json").expanduser()


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


def _bool(name: str, default: bool) -> bool:
    return _optional(name, str(default)).lower() in ("1", "true", "yes", "on")


def _two_digits(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or len(raw) > 4:
        raise RuntimeError(f"{name} must be digits, like 07 or 29")
    return raw[-2:].zfill(2)


def database_url() -> str:
    return _optional("CAMPSITE_DATABASE_URL", f"sqlite:///{DATA_DIR / 'campsites.db'}")


@dataclass(frozen=True)
class Settings:
    database_url: str
    # recreation.gov login, used when the saved session has expired.
    recgov_email: str | None
    recgov_password: str | None
    # The signed-in browser session `campsite-booker-login` saves and every
    # hold reuses (cookies and local storage). Mode 600: it's a login.
    session_file: Path
    # Where a failed hold's screenshot and page HTML go.
    artifacts_dir: Path
    headless: bool
    # Watches' dates are local dates; "today" is here.
    timezone: ZoneInfo
    # Click the final Confirm. An operator kill switch: off, every checkout
    # stops at "Review and Confirm" (a dry run) and the site stays in the cart.
    submit_payment: bool = False
    # recreation.gov's digital wallet sometimes asks for the saved card's
    # last 4 digits (and expiry) before using it. Not secret enough to keep
    # out of .env, and never typed anywhere else; a wrong answer counts
    # towards a 24-hour wallet lock, so it's tried once.
    card_last4: str | None = None
    card_exp_month: str | None = None  # MM
    card_exp_year: str | None = None  # YY
    # Group size for order details, when a watch doesn't give one.
    group_size: int = 2
    # Seconds between checks while waiting on a release.
    release_poll_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=database_url(),
            recgov_email=os.environ.get("RECGOV_EMAIL", "").strip() or None,
            recgov_password=os.environ.get("RECGOV_PASSWORD", "") or None,
            session_file=Path(
                _optional("CAMPSITE_SESSION_FILE", str(DATA_DIR / "session.json"))
            ).expanduser(),
            artifacts_dir=Path(
                _optional("CAMPSITE_ARTIFACTS_DIR", str(DATA_DIR / "artifacts"))
            ).expanduser(),
            headless=_bool("CAMPSITE_HEADLESS", True),
            timezone=ZoneInfo(_optional("CAMPSITE_TIMEZONE", "America/Los_Angeles")),
            submit_payment=_bool("CAMPSITE_SUBMIT_PAYMENT", False),
            card_last4=os.environ.get("CAMPSITE_CARD_LAST4", "").strip() or None,
            card_exp_month=_two_digits("CAMPSITE_CARD_EXP_MONTH"),
            card_exp_year=_two_digits("CAMPSITE_CARD_EXP_YEAR"),
            group_size=max(_int("CAMPSITE_GROUP_SIZE", 2), 1),
            # Bursts last minutes at most, but not every half second.
            release_poll_seconds=max(_int("CAMPSITE_RELEASE_POLL_SECONDS", 2), 1),
        )


@dataclass(frozen=True)
class GmailSettings:
    """Where the Gmail sign-in lives, for recreation.gov's alert emails."""

    # A Desktop OAuth client from Google Cloud: this app's own, or else
    # pay-my-bills' (same project, same consent screen).
    client_secrets: Path
    # Written by `campsite-booker-gmail-login`, refreshed in place after.
    token_file: Path

    @classmethod
    def from_env(cls) -> "GmailSettings":
        own = CONFIG_DIR / "client_secret.json"
        default = (
            own
            if own.exists() or not PAY_MY_BILLS_CLIENT.exists()
            else (PAY_MY_BILLS_CLIENT)
        )
        return cls(
            client_secrets=Path(
                _optional("CAMPSITE_GMAIL_CLIENT_SECRETS", str(default))
            ).expanduser(),
            token_file=Path(
                _optional(
                    "CAMPSITE_GMAIL_TOKEN_FILE", str(CONFIG_DIR / "gmail-token.json")
                )
            ).expanduser(),
        )


@dataclass(frozen=True)
class BotSettings:
    """What the Telegram watcher needs on top of `Settings`."""

    telegram_token: str
    # Only these Telegram users are answered, and every alert goes to all of
    # them (a user's private chat id is their user id).
    allowed_user_ids: frozenset[int]
    # Seconds between checks of every active watch.
    poll_seconds: int
    # Seconds between looks at Gmail for recreation.gov's alert emails; 0
    # turns the email fallback off.
    gmail_poll_seconds: int = 30

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
            # recreation.gov is a public service: no faster than every 30 s.
            poll_seconds=max(_int("CAMPSITE_POLL_SECONDS", 60), 30),
            gmail_poll_seconds=_gmail_poll_seconds(),
        )


def _gmail_poll_seconds() -> int:
    seconds = _int("CAMPSITE_GMAIL_POLL_SECONDS", 30)
    # Gmail's API allows far more, but every look is a list call.
    return 0 if seconds <= 0 else max(seconds, 10)
