"""The adapter contract: every course resolves to an adapter that fits it."""

from datetime import date

import pytest
from tee_time_booker.config import Account, Credentials, PaymentCard
from tee_time_booker.courses import COURSES, SATICOY, SOULE_PARK
from tee_time_booker.platforms import PLATFORMS, build_platform, get_platform
from tee_time_booker.platforms.base import BookingPlatform


def _credentials() -> Credentials:
    return Credentials(
        account=Account(email="a@example.com", password="pw"),
        card=PaymentCard(
            number="4111111111111111",
            expiration_month="01",
            expiration_year="30",
            cvc="123",
            postal_code="93023",
        ),
    )


@pytest.mark.parametrize("course", list(COURSES.values()), ids=list(COURSES))
def test_every_course_has_an_adapter_that_accepts_it(course):
    platform = get_platform(course.platform)
    assert issubclass(platform, BookingPlatform)
    assert isinstance(course, platform.course_type)


@pytest.mark.parametrize("platform", list(PLATFORMS.values()), ids=list(PLATFORMS))
def test_adapters_declare_the_card_fields_their_checkout_needs(platform):
    assert set(platform.required_card_fields) <= set(PaymentCard.ENV_VARS)


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


def test_an_adapter_without_a_card_says_which_variables_are_missing():
    # Listing tee times needs no card, so the check happens at checkout time.
    platform = build_platform(
        SATICOY, Credentials(account=Account("a@example.com", "pw")), session=None
    )
    with pytest.raises(RuntimeError, match="CREDIT_CARD_NUMBER"):
        platform.card


def test_courses_describe_their_own_search():
    criteria = SOULE_PARK.criteria(date(2026, 10, 3))
    prefs = SOULE_PARK.preferences
    assert criteria.target_date == date(2026, 10, 3)
    assert (criteria.players, criteria.holes) == (prefs.players, prefs.holes)
    assert (criteria.earliest, criteria.latest) == (prefs.earliest, prefs.latest)
