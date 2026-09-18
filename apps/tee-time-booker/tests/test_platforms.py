"""The adapter contract: every course resolves to an adapter that fits it."""

from datetime import date, time

import pytest
from tee_time_booker.config import Account, Credentials, PaymentDetails
from tee_time_booker.courses import SATICOY, SEED_COURSES, SOULE_PARK
from tee_time_booker.models import Preferences
from tee_time_booker.platforms import PLATFORMS, build_platform, get_platform
from tee_time_booker.platforms.base import BookingPlatform


def _credentials() -> Credentials:
    return Credentials(
        account=Account(email="a@example.com", password="pw"),
        payment=PaymentDetails(
            number="4111111111111111",
            expiration_month="01",
            expiration_year="30",
            cvc="123",
            postal_code="93023",
        ),
    )


@pytest.mark.parametrize("course", SEED_COURSES, ids=[c.key for c in SEED_COURSES])
def test_every_course_has_an_adapter_that_accepts_it(course):
    platform = get_platform(course.platform)
    assert issubclass(platform, BookingPlatform)
    assert isinstance(course, platform.course_type)


@pytest.mark.parametrize("platform", list(PLATFORMS.values()), ids=list(PLATFORMS))
def test_adapters_declare_the_payment_fields_their_checkout_needs(platform):
    assert set(platform.required_payment_fields) <= set(PaymentDetails.ENV_VARS)


def test_unknown_platform_names_the_known_ones():
    with pytest.raises(ValueError, match="foreup"):
        get_platform("nope")


def test_building_an_adapter_for_the_wrong_course_type_fails_loudly():
    golfnow = get_platform("golfnow")
    with pytest.raises(TypeError, match="GolfNowCourse"):
        golfnow(SOULE_PARK, _credentials(), session=None)


def test_build_platform_picks_the_adapter_named_by_the_course():
    platform = build_platform(SATICOY, _credentials(), session=None)
    assert platform.name == "golfnow"
    assert platform.course is SATICOY


def test_an_adapter_without_payment_details_says_which_variables_are_missing():
    # Listing tee times needs no payment details; the check happens at checkout.
    platform = build_platform(
        SATICOY, Credentials(account=Account("a@example.com", "pw")), session=None
    )
    with pytest.raises(RuntimeError, match="CREDIT_CARD_STATE"):
        platform.payment


def test_preferences_describe_their_own_search():
    prefs = Preferences(
        target_weekday=5,
        weeks_ahead=3,
        earliest=time(8, 0),
        latest=time(14, 0),
        holes=18,
        players=4,
    )
    criteria = prefs.criteria(date(2026, 10, 3))
    assert criteria.target_date == date(2026, 10, 3)
    assert (criteria.players, criteria.holes) == (prefs.players, prefs.holes)
    assert (criteria.earliest, criteria.latest) == (prefs.earliest, prefs.latest)


def test_platform_config_is_only_the_adapters_identifiers():
    assert SOULE_PARK.platform_config() == {
        "course_id": "19620",
        "booking_class": "4718",
        "booking_class_name": "Regular",
        "schedule_id": "1945",
    }
