"""Runtime settings and credentials.

Booking preferences live in `courses.py`; anything secret or environment
specific comes from the environment / `.env` and lands here.
"""

import os
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from tee_time_booker.courses import DEFAULT_COURSE, CourseConfig

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
    def from_env(cls, course: CourseConfig) -> "Account":
        return cls(
            email=_required(course.email_env),
            password=_required(course.password_env),
        )


@dataclass(frozen=True)
class PaymentCard:
    """The card entered at checkout.

    Sites ask for different subsets of this: foreUP wants the number, expiry,
    CVC and postal code; GolfNow also wants a cardholder name, billing address
    and phone number. Each adapter declares the fields it needs
    (`BookingPlatform.required_card_fields`) and `from_env` only insists on
    those, so a run doesn't fail over a field its site never asks for.
    """

    number: str
    expiration_month: str  # MM
    expiration_year: str  # YY
    cvc: str
    postal_code: str
    name: str | None = None
    billing_address: str | None = None
    phone: str | None = None

    # Field name -> environment variable holding it.
    ENV_VARS = {
        "number": "CREDIT_CARD_NUMBER",
        "expiration_month": "CREDIT_CARD_EXPIRATION_MONTH",
        "expiration_year": "CREDIT_CARD_EXPIRATION_YEAR",
        "cvc": "CREDIT_CARD_CVC",
        "postal_code": "CREDIT_CARD_POSTAL_CODE",
        "name": "CREDIT_CARD_NAME",
        "billing_address": "CREDIT_CARD_BILLING_ADDRESS",
        "phone": "PHONE_NUMBER",
    }

    @classmethod
    def from_env(cls, required: tuple[str, ...]) -> "PaymentCard":
        unknown = set(required) - set(cls.ENV_VARS)
        if unknown:
            raise ValueError(f"Not card fields: {', '.join(sorted(unknown))}")
        values: dict[str, str | None] = {}
        for f in fields(cls):
            env_var = cls.ENV_VARS[f.name]
            values[f.name] = (
                _required(env_var)
                if f.name in required
                else os.environ.get(env_var, "").strip() or None
            )
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Credentials:
    """A login, and the card to pay with once there is something to pay for.

    `card` is None for runs that only look (listing tee times), so a search
    doesn't need card details in the environment.
    """

    account: Account
    card: PaymentCard | None = None


@dataclass(frozen=True)
class Settings:
    course_key: str
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
            course_key=os.environ.get("TEE_TIME_COURSE", "").strip() or DEFAULT_COURSE,
            submit_payment=_env_flag("TEE_TIME_SUBMIT_PAYMENT", default=False),
            headless=_env_flag("TEE_TIME_HEADLESS", default=True),
            log_path=Path(os.environ.get("TEE_TIME_LOG_PATH", "log.csv")),
            artifacts_dir=Path(os.environ.get("TEE_TIME_ARTIFACTS_DIR", "artifacts")),
            target_date_override=date.fromisoformat(raw_date) if raw_date else None,
        )
