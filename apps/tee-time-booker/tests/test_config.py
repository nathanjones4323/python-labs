import uuid

import pytest
from tee_time_booker.config import Account, PaymentDetails, Settings

CARD_ENV = {
    "CREDIT_CARD_NUMBER": "4111111111111111",
    "CREDIT_CARD_EXPIRATION_MONTH": "01",
    "CREDIT_CARD_EXPIRATION_YEAR": "30",
    "CREDIT_CARD_CVC": "123",
    "CREDIT_CARD_POSTAL_CODE": "93023",
}


@pytest.fixture
def env(monkeypatch):
    overrides = (
        "TEE_TIME_PLAN",
        "TEE_TIME_SUBMIT_PAYMENT",
        "TEE_TIME_HEADLESS",
        "DATABASE_URL",
    )
    for name in (*PaymentDetails.ENV_VARS.values(), *overrides):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_each_site_account_reads_its_own_login(env):
    env.setenv("SOULE_EMAIL", "soule@example.com")
    env.setenv("SOULE_PASSWORD", "soule-pw")
    env.setenv("GOLFNOW_EMAIL", "gn@example.com")
    env.setenv("GOLFNOW_PASSWORD", "gn-pw")
    assert Account.from_env("SOULE") == Account("soule@example.com", "soule-pw")
    assert Account.from_env("GOLFNOW") == Account("gn@example.com", "gn-pw")


def test_missing_login_names_the_variable(env):
    env.delenv("SOULE_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="SOULE_EMAIL"):
        Account.from_env("SOULE")


def test_payment_details_only_insist_on_the_fields_the_site_asks_for(env):
    for name, value in CARD_ENV.items():
        env.setenv(name, value)
    details = PaymentDetails.from_env(("number", "cvc"))
    assert details.number == CARD_ENV["CREDIT_CARD_NUMBER"]
    # The GolfNow-only field stays unset rather than failing a foreUP run.
    assert details.state is None


def test_a_golfnow_run_needs_no_card_number(env):
    env.setenv("CREDIT_CARD_STATE", "CA")
    env.setenv("CREDIT_CARD_POSTAL_CODE", "93023")
    details = PaymentDetails.from_env(("state", "postal_code"))
    assert (details.state, details.postal_code) == ("CA", "93023")
    assert details.number is None and details.cvc is None


def test_payment_details_fail_when_a_required_field_is_missing(env):
    for name, value in CARD_ENV.items():
        env.setenv(name, value)
    with pytest.raises(RuntimeError, match="CREDIT_CARD_STATE"):
        PaymentDetails.from_env(("number", "state"))


def test_settings_default_to_a_dry_run_with_no_plan(env):
    settings = Settings.from_env()
    assert settings.plan_id is None
    assert settings.submit_payment is False
    assert settings.headless is True
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        settings.require_database_url()


def test_settings_read_the_plan_and_database_from_the_environment(env):
    plan = uuid.uuid4()
    env.setenv("TEE_TIME_PLAN", str(plan))
    env.setenv("DATABASE_URL", "postgresql://u:p@db/tee")
    settings = Settings.from_env()
    assert settings.plan_id == plan
    assert settings.require_database_url() == "postgresql://u:p@db/tee"
