"""Runtime settings and credentials.

Who books what lives in the database (`tee_time_booker.db`); anything secret
or environment specific comes from the environment / `.env` and lands here.
"""

import os
import uuid
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Account:
    """A login on a booking site."""

    email: str
    password: str

    @classmethod
    def from_env(cls, env_prefix: str) -> "Account":
        """The login held in `<PREFIX>_EMAIL` / `<PREFIX>_PASSWORD`.

        A site account row (`db.SiteAccount`) names its prefix; the secrets
        themselves stay out of the database until there's an encryption story.
        """
        return cls(
            email=_required(f"{env_prefix}_EMAIL"),
            password=_required(f"{env_prefix}_PASSWORD"),
        )


@dataclass(frozen=True)
class PaymentDetails:
    """What a checkout asks for beyond the login.

    Sites differ in how much that is. foreUP (Soule Park) has no card on file,
    so it wants a card typed in every time: number, expiry, CVC and postal
    code. GolfNow pays with the card saved on the account and only asks for
    the billing state and postal code it doesn't keep on the profile — so a
    GolfNow run never holds a card number. Each adapter declares the fields
    it needs (`BookingPlatform.required_payment_fields`) and `from_env` only
    insists on those, so a run doesn't fail over a field its site never asks
    for.
    """

    number: str | None = None
    expiration_month: str | None = None  # MM
    expiration_year: str | None = None  # YY
    cvc: str | None = None
    postal_code: str | None = None
    state: str | None = None  # two-letter code, e.g. CA

    # Field name -> environment variable holding it.
    ENV_VARS = {
        "number": "CREDIT_CARD_NUMBER",
        "expiration_month": "CREDIT_CARD_EXPIRATION_MONTH",
        "expiration_year": "CREDIT_CARD_EXPIRATION_YEAR",
        "cvc": "CREDIT_CARD_CVC",
        "postal_code": "CREDIT_CARD_POSTAL_CODE",
        "state": "CREDIT_CARD_STATE",
    }

    @classmethod
    def from_env(cls, required: tuple[str, ...]) -> "PaymentDetails":
        unknown = set(required) - set(cls.ENV_VARS)
        if unknown:
            raise ValueError(f"Not payment fields: {', '.join(sorted(unknown))}")
        values: dict[str, str | None] = {}
        for f in fields(cls):
            env_var = cls.ENV_VARS[f.name]
            values[f.name] = (
                _required(env_var)
                if f.name in required
                else os.environ.get(env_var, "").strip() or None
            )
        return cls(**values)


@dataclass(frozen=True)
class Credentials:
    """A login, and what checkout will ask for once there is something to pay.

    `payment` is None for runs that only look (listing tee times), so a search
    doesn't need payment details in the environment.
    """

    account: Account
    payment: PaymentDetails | None = None


@dataclass(frozen=True)
class Settings:
    # Where runs are recorded. None only works for commands that don't touch
    # the database; `require_database_url` names the variable otherwise.
    database_url: str | None
    # The booking plan a scheduled run books (TEE_TIME_PLAN, or --plan).
    plan_id: uuid.UUID | None
    # Operator kill switch: False turns every run into a dry run, whatever
    # the request asks for.
    submit_payment: bool
    headless: bool
    artifacts_dir: Path
    # Testing aid: book this date (YYYY-MM-DD) instead of the computed one.
    target_date_override: date | None

    @classmethod
    def from_env(cls) -> "Settings":
        raw_date = os.environ.get("TEE_TIME_TARGET_DATE", "").strip()
        raw_plan = os.environ.get("TEE_TIME_PLAN", "").strip()
        return cls(
            database_url=os.environ.get("DATABASE_URL", "").strip() or None,
            plan_id=uuid.UUID(raw_plan) if raw_plan else None,
            submit_payment=_env_flag("TEE_TIME_SUBMIT_PAYMENT", default=False),
            headless=_env_flag("TEE_TIME_HEADLESS", default=True),
            artifacts_dir=Path(os.environ.get("TEE_TIME_ARTIFACTS_DIR", "artifacts")),
            target_date_override=date.fromisoformat(raw_date) if raw_date else None,
        )

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError("Missing required environment variable DATABASE_URL")
        return self.database_url


@dataclass(frozen=True)
class BotSettings:
    """What the Telegram bot needs on top of `Settings`.

    Kept apart so the CLI and the scheduled run never need a bot token.
    """

    telegram_token: str
    # Only these Telegram users are answered; everyone else is ignored.
    allowed_user_ids: frozenset[int]
    # The `users` row the bot books as (one person per install).
    user_email: str
    model: str
    effort: str
    whisper_model: str

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
        # The Anthropic SDK reads ANTHROPIC_API_KEY itself; fail here, at
        # start-up, rather than on the first message.
        _required("ANTHROPIC_API_KEY")
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=ids,
            user_email=_required("TEE_TIME_USER_EMAIL"),
            model=os.environ.get("TEE_TIME_BOT_MODEL", "").strip()
            or "claude-sonnet-5-5",
            effort=os.environ.get("TEE_TIME_BOT_EFFORT", "").strip() or "medium",
            whisper_model=os.environ.get("TEE_TIME_WHISPER_MODEL", "").strip()
            or "base.en",
        )
