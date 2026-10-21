"""First-time setup for running the booker yourself, with the Telegram bot.

    cd apps/tee-time-booker
    uv run --package tee-time-booker python -m tee_time_booker.setup

Asks for what the bot and your courses need, writes it to `.env` (readable
only by you), creates a local SQLite database, and loads the courses and a
default plan for each course you book. Rerunning it is safe: values already
in `.env` are kept, never asked for again, so it also fills in whatever a
first run skipped.

Nothing you type here leaves this machine. The logins and card stay in
`.env`, where the booker reads them when it signs in or pays.
"""

import argparse
import getpass
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from tee_time_booker.courses import SEED_COURSES
from tee_time_booker.db import make_session_factory
from tee_time_booker.db.service import upsert_course
from tee_time_booker.seed import ACCOUNT_ENV_PREFIXES, DEFAULT_PLANS, seed_user

APP_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Question:
    name: str
    prompt: str
    secret: bool = False
    default: str | None = None
    help: str | None = None


BOT_QUESTIONS = [
    Question(
        "TEE_TIME_USER_EMAIL",
        "Your email (labels your bookings; nothing is sent to it)",
    ),
    Question(
        "TELEGRAM_BOT_TOKEN",
        "Bot token",
        secret=True,
        help=(
            "Create your bot: in Telegram, message @BotFather, send /newbot and "
            "follow along. It replies with a token like 123456:ABC-DEF..."
        ),
    ),
    Question(
        "TELEGRAM_ALLOWED_USER_IDS",
        "Your Telegram user id",
        help=(
            "The bot answers only you. Message @userinfobot in Telegram; it "
            "replies with your numeric id."
        ),
    ),
    Question(
        "ANTHROPIC_API_KEY",
        "Anthropic API key",
        secret=True,
        help="Claude reads your messages. Create a key at https://console.anthropic.com/",
    ),
]

FOREUP_CARD = [
    Question(
        "CREDIT_CARD_NUMBER",
        "Card number",
        secret=True,
        help="foreUP has no card on file, so checkout types one in every time.",
    ),
    Question("CREDIT_CARD_EXPIRATION_MONTH", "Card expiry month (MM)"),
    Question("CREDIT_CARD_EXPIRATION_YEAR", "Card expiry year (YY)"),
    Question("CREDIT_CARD_CVC", "Card CVC", secret=True),
    Question("CREDIT_CARD_POSTAL_CODE", "Card billing postal code"),
]

GOLFNOW_BILLING = [
    Question(
        "CREDIT_CARD_STATE",
        "Billing state (two letters, e.g. CA)",
        help=(
            "GolfNow pays with the card saved on your GolfNow account (save one "
            "there first), but asks for its billing state and postal code."
        ),
    ),
    Question("CREDIT_CARD_POSTAL_CODE", "Card billing postal code"),
]


def ask(question: Question) -> str:
    if question.help:
        # Flushed: getpass writes its prompt straight to the terminal.
        print(f"\n{question.help}", flush=True)
    suffix = f" [{question.default}]" if question.default else ""
    while True:
        prompt = f"{question.prompt}{suffix}: "
        value = (getpass.getpass(prompt) if question.secret else input(prompt)).strip()
        value = value or question.default or ""
        if value:
            return value
        print("  (needed; Ctrl-C to stop and come back later)")


def yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def ask_missing(questions: list[Question], known: dict[str, str]) -> dict[str, str]:
    """Answers for the questions `known` doesn't already answer."""
    answers: dict[str, str] = {}
    for q in questions:
        if known.get(q.name) or answers.get(q.name):
            continue
        answers[q.name] = ask(q)
        known = {**known, **answers}
    return answers


def write_env(path: Path, values: dict[str, str]) -> None:
    """Append `values` to the env file, leaving what's there alone, and make
    it readable by its owner only: it holds logins and maybe a card."""
    existing = path.read_text() if path.exists() else ""
    lines = [] if not existing or existing.endswith("\n") else [""]
    lines += [f"{name}={_quote(value)}" for name, value in values.items()]
    # Created 0600 (and tightened if it already existed) before any secret
    # is written, so it is never readable by others even briefly.
    mode = stat.S_IRUSR | stat.S_IWUSR
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "a") as f:
        f.write("\n".join(lines) + "\n")


def _quote(value: str) -> str:
    # Single quotes: python-dotenv doesn't expand ${...} inside them, and only
    # \\ and \' are escapes there.
    if any(c in value for c in " #'\"\\$"):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return value


def absolute_sqlite_url(url: str, base: Path) -> str:
    """`url` with a relative SQLite path resolved against `base`.

    A relative path is opened from whatever directory the booker runs in, so
    starting the bot from elsewhere would quietly get a new, empty database.
    Other databases, and in-memory SQLite, come back unchanged.
    """
    parsed = make_url(url)
    path = parsed.database
    if parsed.get_backend_name() != "sqlite" or not path or path == ":memory:":
        return url
    if Path(path).is_absolute():
        return url
    return parsed.set(database=str((base / path).resolve())).render_as_string(
        hide_password=False
    )


def migrate(database_url: str) -> None:
    config = Config(APP_DIR / "alembic.ini")
    config.set_main_option("script_location", str(APP_DIR / "migrations"))
    config.cmd_opts = argparse.Namespace(x=[f"url={database_url}"])
    command.upgrade(config, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env-file", type=Path, default=APP_DIR / ".env", help="default: %(default)s"
    )
    args = parser.parse_args(argv)
    env_file: Path = args.env_file

    known = (
        {k: v for k, v in dotenv_values(env_file).items() if v}
        if env_file.exists()
        else {}
    )
    print(f"Setting up {env_file} (values already there are kept).")
    new: dict[str, str] = {}

    database_url = known.get("DATABASE_URL")
    if not database_url:
        new["DATABASE_URL"] = f"sqlite:///{APP_DIR / 'tee_time_booker.db'}"
    elif (
        absolute := absolute_sqlite_url(database_url, env_file.parent)
    ) != database_url:
        # Appended after the relative one, so it's the value that's read.
        new["DATABASE_URL"] = absolute
    new |= ask_missing(BOT_QUESTIONS, known)

    courses = {c.key: c for c in SEED_COURSES}
    print("\nWhich courses do you book?")
    chosen = {
        key
        for key, course in courses.items()
        if key in DEFAULT_PLANS and yes(f"  {course.name} ({course.platform})?")
    }
    for key in sorted(chosen):
        prefix = ACCOUNT_ENV_PREFIXES[key]
        login = [
            Question(
                f"{prefix}_EMAIL",
                f"{courses[key].name} login email",
                help=f"Your {courses[key].platform} account for {courses[key].name}.",
            ),
            Question(
                f"{prefix}_PASSWORD", f"{courses[key].name} password", secret=True
            ),
        ]
        new |= ask_missing(login, {**known, **new})
    platforms = {courses[k].platform for k in chosen}
    if "foreup" in platforms:
        new |= ask_missing(FOREUP_CARD, {**known, **new})
    if "golfnow" in platforms:
        new |= ask_missing(GOLFNOW_BILLING, {**known, **new})
    if not known.get("TEE_TIME_SUBMIT_PAYMENT"):
        # Dry runs until the person has seen one work.
        new["TEE_TIME_SUBMIT_PAYMENT"] = "false"

    if new:
        write_env(env_file, new)
        print(f"\nSaved {len(new)} setting(s) to {env_file} (only you can read it).")
    values = {**known, **new}
    os.environ.update(values)

    print("\nPreparing the database…")
    migrate(values["DATABASE_URL"])
    sessions = make_session_factory(values["DATABASE_URL"])
    with sessions() as session, session.begin():
        for config in SEED_COURSES:
            upsert_course(session, config)
        plans = seed_user(session, values["TEE_TIME_USER_EMAIL"], None, courses, chosen)
        session.flush()
        names = [f"  {p.name}" for p in plans]

    print("Your plans (the bot books these, or any one-off day you ask for):")
    print("\n".join(names) or "  none: no course chosen")
    print(
        "\nStart the bot:\n"
        "  uv run --package tee-time-booker python -m tee_time_booker.bot\n\n"
        "Then message your bot in Telegram. Every run is a dry run (it stops at "
        "checkout)\nuntil you set TEE_TIME_SUBMIT_PAYMENT=true in .env and restart "
        "the bot, so try\none first."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
