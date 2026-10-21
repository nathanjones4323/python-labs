import stat

from dotenv import dotenv_values
from tee_time_booker.setup import Question, absolute_sqlite_url, ask_missing, write_env


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
