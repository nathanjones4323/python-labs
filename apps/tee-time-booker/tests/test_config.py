import pytest
from tee_time_booker.config import Account, PaymentCard, Settings
from tee_time_booker.courses import SATICOY, SOULE_PARK

CARD_ENV = {
    "CREDIT_CARD_NUMBER": "4111111111111111",
    "CREDIT_CARD_EXPIRATION_MONTH": "01",
    "CREDIT_CARD_EXPIRATION_YEAR": "30",
    "CREDIT_CARD_CVC": "123",
    "CREDIT_CARD_POSTAL_CODE": "93023",
}


@pytest.fixture
def env(monkeypatch):
    overrides = ("TEE_TIME_COURSE", "TEE_TIME_SUBMIT_PAYMENT", "TEE_TIME_HEADLESS")
    for name in (*PaymentCard.ENV_VARS.values(), *overrides):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_each_course_reads_its_own_login(env):
    env.setenv("SOULE_EMAIL", "soule@example.com")
    env.setenv("SOULE_PASSWORD", "soule-pw")
    env.setenv("GOLFNOW_EMAIL", "gn@example.com")
    env.setenv("GOLFNOW_PASSWORD", "gn-pw")
    assert Account.from_env(SOULE_PARK) == Account("soule@example.com", "soule-pw")
    assert Account.from_env(SATICOY) == Account("gn@example.com", "gn-pw")


def test_missing_login_names_the_variable(env):
    env.delenv("SOULE_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="SOULE_EMAIL"):
        Account.from_env(SOULE_PARK)


def test_card_only_insists_on_the_fields_its_site_asks_for(env):
    for name, value in CARD_ENV.items():
        env.setenv(name, value)
    card = PaymentCard.from_env(("number", "cvc"))
    assert card.number == CARD_ENV["CREDIT_CARD_NUMBER"]
    # GolfNow-only fields stay unset rather than failing a foreUP run.
    assert card.name is None and card.billing_address is None and card.phone is None


def test_card_fails_when_a_required_field_is_missing(env):
    for name, value in CARD_ENV.items():
        env.setenv(name, value)
    with pytest.raises(RuntimeError, match="PHONE_NUMBER"):
        PaymentCard.from_env(("number", "phone"))


def test_settings_default_to_the_default_course_and_a_dry_run(env):
    settings = Settings.from_env()
    assert settings.course_key == SOULE_PARK.key
    assert settings.submit_payment is False
    assert settings.headless is True
