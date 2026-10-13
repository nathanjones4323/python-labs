"""Which database a checkout uses, and that nothing wipes prod."""

from pathlib import Path

import pytest
from date_planner import reset
from date_planner.config import Settings, get_settings, is_prod_database

DEV = "postgresql://postgres:postgres@localhost:5434/date_planner"
PROD = "postgresql://postgres:postgres@localhost:5435/date_planner_prod"


def test_env_local_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"DATABASE_URL={DEV}\n")
    # A worktree: .env only, so dev.
    assert Settings().database_url == DEV
    # The main checkout: .env.local points it at prod.
    (tmp_path / ".env.local").write_text(f"DATABASE_URL={PROD}\n")
    assert Settings().database_url == PROD
    # The environment still beats both.
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/other")
    assert Settings().database_url == "postgresql://h/other"


def test_database_label_hides_the_password() -> None:
    settings = Settings(_env_file=None, DATABASE_URL=PROD)  # type: ignore[call-arg]
    assert settings.database_label() == "localhost:5435/date_planner_prod"


@pytest.mark.parametrize(
    ("url", "prod"),
    [
        (PROD, True),
        (PROD + "/", True),
        (DEV, False),
        ("postgresql://postgres:postgres@localhost:5434/date_planner_test", False),
        ("postgresql://u:p@host/production_notes", False),
    ],
)
def test_is_prod_database(url: str, prod: bool) -> None:
    assert is_prod_database(url) is prod


def test_reset_refuses_prod_even_with_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", PROD)
    monkeypatch.setattr("sys.argv", ["date-planner-reset", "--force"])
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="it's prod"):
            reset.main()
    finally:
        get_settings.cache_clear()
