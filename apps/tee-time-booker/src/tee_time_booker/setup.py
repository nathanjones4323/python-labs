"""First-time setup for running the booker yourself, with the Telegram bot.

    cd apps/tee-time-booker
    uv run --package tee-time-booker python -m tee_time_booker.setup

Asks for what the bot and your courses need, writes it to `.env` (readable
only by you), creates a local SQLite database, and loads the courses, your
login on each booking site you use. It makes no plans: the bot sets up a
weekly booking only when you ask for one.
Rerunning it is safe: values already in `.env` are kept, never asked for
again, so it also fills in whatever a first run skipped.

Nothing you type here leaves this machine. The logins and card stay in
`.env`, where the booker reads them when it signs in or pays.
"""

import argparse
import asyncio
import getpass
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from tee_time_booker.config import DEFAULT_CLAUDE_MODEL, OLLAMA_URL
from tee_time_booker.courses import SEED_COURSES, CourseConfig
from tee_time_booker.db import make_session_factory
from tee_time_booker.db.service import upsert_course
from tee_time_booker.platforms import PLATFORMS
from tee_time_booker.seed import account_env_prefix, seed_user

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
]

ANTHROPIC_KEY = Question(
    "ANTHROPIC_API_KEY",
    "Anthropic API key",
    secret=True,
    help="Claude reads your messages. Create a key at https://console.anthropic.com/",
)


@dataclass(frozen=True)
class FreeApi:
    """A hosted, OpenAI-compatible API with a free tier. Free models come and
    go, so the model is only a suggestion the person can change."""

    name: str
    base_url: str
    model: str
    key_url: str
    note: str


FREE_APIS = [
    FreeApi(
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        # Of the free models tried (2026-10), the one that followed the
        # bot's rules best; minimax/minimax-m3.1-flash:free also worked.
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "https://openrouter.ai/settings/keys",
        "The suggested model is NVIDIA's free trial, which logs prompts: it "
        "sees your email and bookings, never your logins or card. If it's "
        "busy, try minimax/minimax-m3.1-flash:free (free models end in :free, "
        "at openrouter.ai/models).",
    ),
    FreeApi(
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-3.8-flash",
        "https://aistudio.google.com/apikey",
        "On the free tier Google may use your messages to improve its products.",
    ),
    FreeApi(
        "OpenCode Zen",
        "https://opencode.ai/inference/openai/v1",
        "glm-5.3-flash-free",
        "https://opencode.ai/console",
        "Its key needs billing details on file; the -free models are free "
        "for a limited time (opencode.ai/docs/zen).",
    ),
]

MODEL_CHOICES = """
Which AI reads your messages?
  1) A model on this computer: free and private (needs Ollama, a big download)
  2) A free hosted model: OpenRouter, Google Gemini or OpenCode Zen (needs their key)
  3) Claude: the best results; needs an Anthropic API key with credits"""

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


def choose(prompt: str, count: int, default: int = 1) -> int:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip() or str(default)
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer)
        print(f"  (a number from 1 to {count})")


def choose_model(known: dict[str, str]) -> dict[str, str]:
    """The model settings, asked for unless `.env` already picked one (a
    Claude key from before the choice existed counts)."""
    if known.get("TEE_TIME_BOT_PROVIDER") or known.get("ANTHROPIC_API_KEY"):
        return {}
    print(MODEL_CHOICES)
    choice = choose("Choose", 3)
    if choice == 1:
        values = choose_local_model()
        if values is not None:
            return values
        print("\nThen use a free hosted model instead (or Ctrl-C and come back).")
        choice = 2
    if choice == 2:
        return choose_free_api()
    while True:
        key = ask(ANTHROPIC_KEY)
        if works("anthropic", DEFAULT_CLAUDE_MODEL, key=key) or not yes(
            "Enter the key again?"
        ):
            break
    return {
        "TEE_TIME_BOT_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": key,
        # Written out so switching back from another provider drops its model.
        "TEE_TIME_BOT_MODEL": DEFAULT_CLAUDE_MODEL,
    }


def choose_local_model() -> dict[str, str] | None:
    """Pick the strongest model this machine runs and download it; None when
    the machine is too small or the person would rather not."""
    from tee_time_booker.bot import local_model

    machine = local_model.detect_machine()
    model = local_model.pick_model(machine.memory_gb)
    print(f"\nFound {machine.memory_gb:.0f} GB of memory ({machine.accelerator}).")
    if model is None:
        smallest = local_model.MODELS[-1]
        print(f"The smallest model needs about {smallest.needs_gb:.0f} GB.")
        return None
    if machine.accelerator == "CPU only":
        print("No GPU found, so replies will be slow (a minute or more).")
    print(f"Best fit: {model.tag} (a {model.download_gb:g} GB download).")
    while not local_model.ollama_running():
        print(
            "\nOllama runs the model. Install it from https://ollama.com/download "
            "(or `brew install ollama`) and start it."
        )
        if not yes("Ollama is running now; check again?"):
            return None
    if not yes(f"Download {model.tag}?"):
        return None

    def progress(status: str, done: float | None) -> None:
        line = f"{status} {done:.0%}" if done is not None else status
        print(f"\r  {line:<60}", end="", flush=True)

    local_model.pull(model.tag, progress)
    print()
    alias = local_model.create_alias(model.tag)
    works("local", alias, OLLAMA_URL)
    return {"TEE_TIME_BOT_PROVIDER": "local", "TEE_TIME_BOT_MODEL": alias}


def choose_free_api() -> dict[str, str]:
    print()
    for i, api in enumerate(FREE_APIS, 1):
        print(f"  {i}) {api.name}")
    api = FREE_APIS[choose("Which one", len(FREE_APIS)) - 1]
    key = ask(
        Question(
            "TEE_TIME_BOT_API_KEY",
            f"{api.name} API key",
            secret=True,
            help=f"Create a key at {api.key_url}. {api.note}",
        )
    )
    model = api.model
    while True:
        model = ask(Question("TEE_TIME_BOT_MODEL", "Model", default=model))
        if works("openai", model, api.base_url, key) or not yes("Try another model?"):
            break
    return {
        "TEE_TIME_BOT_PROVIDER": "openai",
        "TEE_TIME_BOT_BASE_URL": api.base_url,
        "TEE_TIME_BOT_API_KEY": key,
        "TEE_TIME_BOT_MODEL": model,
    }


def works(
    provider: str, model: str, base_url: str | None = None, key: str | None = None
) -> bool:
    """Send the model one message, so a key or model that doesn't work shows
    up now rather than on the first message to the bot."""
    from tee_time_booker.bot.llm import check_model

    print(f"\nChecking {model}…", flush=True)
    problem = asyncio.run(check_model(provider, model, base_url, key))
    if problem is None:
        print("  It answered.")
        return True
    print(f"  It didn't answer: {problem[:300]}")
    return False


def ask_missing(questions: list[Question], known: dict[str, str]) -> dict[str, str]:
    """Answers for the questions `known` doesn't already answer."""
    answers: dict[str, str] = {}
    for q in questions:
        if known.get(q.name) or answers.get(q.name):
            continue
        answers[q.name] = ask(q)
        known = {**known, **answers}
    return answers


def logins() -> dict[str, list[CourseConfig]]:
    """The seeded courses by the login that books them (its env prefix): all
    of a site's courses when it signs in site-wide, else one course each."""
    groups: dict[str, list[CourseConfig]] = {}
    for course in SEED_COURSES:
        groups.setdefault(account_env_prefix(course), []).append(course)
    return groups


def login_prompt(courses: list[CourseConfig]) -> str:
    """The question about the login that books `courses`, naming the first
    and counting the rest."""
    site = PLATFORMS[courses[0].platform].display_name
    others = len(courses) - 1
    if not others:
        return f"{site} ({courses[0].name})?"
    return f"{site} ({courses[0].name} and {others} other course{'s' * (others > 1)})?"


def login_questions(prefix: str, courses: list[CourseConfig]) -> list[Question]:
    platform = PLATFORMS[courses[0].platform]
    site = platform.display_name
    if platform.signs_in_per_course:
        name = courses[0].name
        about = f"Your {name} account on {site}: each {site} course has its own."
    else:
        name = site
        about = f"Your {site} account: it books every {site} course."
    return [
        Question(f"{prefix}_EMAIL", f"{name} login email", help=about),
        Question(f"{prefix}_PASSWORD", f"{name} password", secret=True),
    ]


def write_env(path: Path, values: dict[str, str]) -> None:
    """Append `values` to the env file, leaving what's there alone, and make
    it readable by its owner only: it holds logins and maybe a card."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [] if not existing or existing.endswith("\n") else [""]
    lines += [f"{name}={_quote(value)}" for name, value in values.items()]
    # Created 0600 (and tightened if it already existed) before any secret
    # is written, so it is never readable by others even briefly.
    mode = stat.S_IRUSR | stat.S_IWUSR
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    # Windows has no fchmod and no owner/other bits: the file is private to
    # the user through their profile folder's permissions instead.
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    # UTF-8, as python-dotenv reads it, not Windows' default code page.
    with os.fdopen(fd, "a", encoding="utf-8") as f:
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
    parser.add_argument(
        "--model",
        action="store_true",
        help="only choose the AI model again (e.g. to switch to a free one)",
    )
    args = parser.parse_args(argv)
    env_file: Path = args.env_file
    if args.model:
        values = choose_model({})
        write_env(env_file, values)
        print(f"\nSaved to {env_file}. Restart the bot to use it.")
        return 0

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
    new |= choose_model(known)

    print("\nWhich booking sites do you use?")
    groups = logins()
    chosen = {
        prefix
        for prefix, courses in groups.items()
        if yes(f"  {login_prompt(courses)}")
    }
    for prefix in (p for p in groups if p in chosen):
        new |= ask_missing(login_questions(prefix, groups[prefix]), {**known, **new})
    platforms = {groups[prefix][0].platform for prefix in chosen}
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
        courses = {c.key: c for c in SEED_COURSES}
        seed_user(session, values["TEE_TIME_USER_EMAIL"], None, courses, chosen)

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
