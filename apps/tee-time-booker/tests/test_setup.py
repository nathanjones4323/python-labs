import os
import stat
from dataclasses import replace

from dotenv import dotenv_values
from sqlalchemy import select
from tee_time_booker import seed, setup
from tee_time_booker.bot import local_model
from tee_time_booker.courses import OLIVAS_LINKS, SATICOY, SEED_COURSES, SOULE_PARK
from tee_time_booker.db import make_session_factory
from tee_time_booker.db.models import BookingPlan, SiteAccount
from tee_time_booker.setup import (
    Question,
    absolute_sqlite_url,
    ask_missing,
    choose_model,
    write_env,
)


def test_written_values_read_back_exactly_and_only_the_owner_can_read(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KEEP=me")  # no trailing newline
    secrets = {
        "PLAIN": "abc123",
        "SPACED": "gn pass#1",
        "QUOTES": 'it\'s "quoted" $HOME',
        "BACKSLASH": "a\\b\\'c",
    }
    write_env(env, secrets)

    assert dotenv_values(env) == {"KEEP": "me", **secrets}
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_env_is_written_where_there_is_no_fchmod(tmp_path, monkeypatch):
    # Windows: os.fchmod doesn't exist.
    monkeypatch.delattr("os.fchmod")
    env = tmp_path / ".env"
    write_env(env, {"GOLFNOW_PASSWORD": "pässword"})
    assert dotenv_values(env) == {"GOLFNOW_PASSWORD": "pässword"}


def test_only_unanswered_questions_are_asked(monkeypatch):
    asked = []
    monkeypatch.setattr(
        "tee_time_booker.setup.ask", lambda q: asked.append(q.name) or "new"
    )
    answers = ask_missing(
        [Question("SET", "?"), Question("UNSET", "?"), Question("UNSET", "again?")],
        {"SET": "old"},
    )
    assert answers == {"UNSET": "new"}
    assert asked == ["UNSET"]


def test_relative_sqlite_paths_are_made_absolute(tmp_path):
    assert absolute_sqlite_url("sqlite:///tee.db", tmp_path) == (
        f"sqlite:///{tmp_path.resolve() / 'tee.db'}"
    )
    for unchanged in (
        "sqlite:////abs/tee.db",
        "sqlite://",
        "sqlite:///:memory:",
        "postgresql://u:p@localhost:5433/tee_time_booker",
    ):
        assert absolute_sqlite_url(unchanged, tmp_path) == unchanged


def _answers(monkeypatch, *answers, working=None):
    """Script `input`, `getpass` and `ask` alike, in order. Models answer the
    check unless `working` (the models that do) says otherwise."""
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda _="": queue.pop(0))
    monkeypatch.setattr(setup, "ask", lambda q: queue.pop(0) or q.default)
    checked = []

    def works(provider, model, base_url=None, key=None):
        checked.append(model)
        return working is None or model in working

    monkeypatch.setattr(setup, "works", works)
    return checked


def test_a_model_that_does_not_answer_can_be_swapped(monkeypatch):
    # OpenRouter, a key, the suggested model (fails), "try another", a second.
    checked = _answers(
        monkeypatch, "2", "1", "sk", "", "y", "x/y:free", working={"x/y:free"}
    )
    values = choose_model({})
    assert checked == [setup.FREE_APIS[0].model, "x/y:free"]
    assert values["TEE_TIME_BOT_MODEL"] == "x/y:free"


def test_a_model_that_does_not_answer_can_be_kept_anyway(monkeypatch):
    _answers(monkeypatch, "2", "1", "sk", "", "n", working=set())
    assert choose_model({})["TEE_TIME_BOT_MODEL"] == setup.FREE_APIS[0].model


def test_a_model_already_chosen_is_not_asked_again(monkeypatch):
    _answers(monkeypatch)  # any question would fail on the empty queue
    assert choose_model({"ANTHROPIC_API_KEY": "k"}) == {}
    assert choose_model({"TEE_TIME_BOT_PROVIDER": "local"}) == {}


def test_a_local_model_is_downloaded_and_given_a_wide_context(monkeypatch):
    _answers(monkeypatch, "", "y")  # the default choice, then "download it"
    monkeypatch.setattr(
        local_model, "detect_machine", lambda: local_model.Machine(36, "Apple Silicon")
    )
    monkeypatch.setattr(local_model, "ollama_running", lambda: True)
    pulled = []
    monkeypatch.setattr(local_model, "pull", lambda tag, progress: pulled.append(tag))
    monkeypatch.setattr(local_model, "create_alias", lambda tag: "tee-time-booker")

    assert choose_model({}) == {
        "TEE_TIME_BOT_PROVIDER": "local",
        "TEE_TIME_BOT_MODEL": "tee-time-booker",
    }
    assert pulled == [local_model.pick_model(36).tag]


def test_a_machine_too_small_falls_back_to_a_free_api(monkeypatch):
    _answers(monkeypatch, "1", "1", "sk-free", "")
    monkeypatch.setattr(
        local_model, "detect_machine", lambda: local_model.Machine(4, "CPU only")
    )
    values = choose_model({})
    assert values["TEE_TIME_BOT_PROVIDER"] == "openai"
    assert values["TEE_TIME_BOT_BASE_URL"] == setup.FREE_APIS[0].base_url
    assert values["TEE_TIME_BOT_MODEL"] == setup.FREE_APIS[0].model
    assert values["TEE_TIME_BOT_API_KEY"] == "sk-free"


def test_choosing_claude_resets_the_model(monkeypatch):
    _answers(monkeypatch, "3", "sk-ant")
    assert choose_model({}) == {
        "TEE_TIME_BOT_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant",
        "TEE_TIME_BOT_MODEL": "claude-sonnet-5-5",
    }


def test_golfnow_is_one_login_and_each_foreup_course_its_own(monkeypatch):
    other_foreup = replace(SOULE_PARK, key="other-park", name="Other Park")
    monkeypatch.setattr(
        setup, "SEED_COURSES", (SOULE_PARK, SATICOY, other_foreup, OLIVAS_LINKS)
    )
    groups = setup.logins()
    assert {prefix: [c.key for c in cs] for prefix, cs in groups.items()} == {
        "SOULE": ["soule-park"],
        "GOLFNOW": ["saticoy", OLIVAS_LINKS.key],
        "OTHER_PARK": ["other-park"],
    }
    assert setup.login_prompt(groups["SOULE"]) == "foreUP (Soule Park)?"
    assert setup.login_prompt(groups["OTHER_PARK"]) == "foreUP (Other Park)?"
    assert setup.login_prompt(groups["GOLFNOW"]) == (
        f"GolfNow ({SATICOY.name} and 1 other course)?"
    )
    soule = setup.login_questions("SOULE", groups["SOULE"])
    assert [q.prompt for q in soule] == [
        "Soule Park login email",
        "Soule Park password",
    ]
    golfnow = setup.login_questions("GOLFNOW", groups["GOLFNOW"])
    assert [q.prompt for q in golfnow] == ["GolfNow login email", "GolfNow password"]


def test_the_seeded_golfnow_courses_are_counted():
    golfnow = sum(c.platform == "golfnow" for c in SEED_COURSES)
    assert setup.login_prompt(setup.logins()["GOLFNOW"]) == (
        f"GolfNow ({SATICOY.name} and {golfnow - 1} other courses)?"
    )


def _run_setup(tmp_path, monkeypatch, *answers):
    """Run setup with the bot and model already in `.env`, so only the site
    questions are asked, and return the `.env` it leaves and the database."""
    env = tmp_path / ".env"
    database_url = f"sqlite:///{tmp_path / 'tee.db'}"
    env.write_text(
        f"DATABASE_URL={database_url}\n"
        "TEE_TIME_USER_EMAIL=me@example.com\n"
        "TELEGRAM_BOT_TOKEN=t\n"
        "TELEGRAM_ALLOWED_USER_IDS=1\n"
        "ANTHROPIC_API_KEY=k\n"
    )
    # main() exports the values; keep them out of the other tests.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    _answers(monkeypatch, *answers)
    assert setup.main(["--env-file", str(env)]) == 0
    return dotenv_values(env), make_session_factory(database_url)


def test_golfnow_alone_gets_one_login_and_only_golfnow_plans(tmp_path, monkeypatch):
    # No to foreUP; yes to GolfNow, its login, then its billing state and zip.
    env, sessions = _run_setup(
        tmp_path, monkeypatch, "n", "y", "gn@example.com", "pw", "CA", "93001"
    )
    assert env["GOLFNOW_EMAIL"] == "gn@example.com"
    assert env["GOLFNOW_PASSWORD"] == "pw"
    assert "SOULE_EMAIL" not in env and "CREDIT_CARD_NUMBER" not in env
    with sessions() as session:
        accounts = session.scalars(select(SiteAccount)).all()
        assert [(a.platform, a.env_prefix, a.login_email) for a in accounts] == [
            ("golfnow", "GOLFNOW", "gn@example.com")
        ]
        plans = session.scalars(select(BookingPlan)).all()
        assert {p.course.platform for p in plans} == {"golfnow"}


def test_a_site_without_a_default_plan_still_gets_its_login(tmp_path, monkeypatch):
    monkeypatch.setattr(seed, "DEFAULT_PLANS", {})
    _, sessions = _run_setup(
        tmp_path, monkeypatch, "n", "y", "gn@example.com", "pw", "CA", "93001"
    )
    with sessions() as session:
        assert [a.platform for a in session.scalars(select(SiteAccount))] == ["golfnow"]
        assert session.scalars(select(BookingPlan)).all() == []
