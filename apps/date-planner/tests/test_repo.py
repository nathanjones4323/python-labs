"""The repo layer against a real Postgres: upserts, stops, derived status,
listings, the travel cache."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from date_planner.booking.channels import BookingChannel, WindowUnit
from date_planner.booking.detect import Detection
from date_planner.booking.window import BookingWindow
from date_planner.db import repo
from date_planner.db.models import (
    Couple,
    DateStatus,
    RatingSource,
    ReservationNeed,
    StopBookingStatus,
    StopKind,
    TravelMode,
    VenueType,
)
from date_planner.errors import (
    AmbiguousVenue,
    DuplicateVenue,
    IncompleteRating,
    InvalidTransition,
    NotFound,
)
from date_planner.schemas import (
    CoupleProfileIn,
    DatePatch,
    MemberIn,
    PreferenceEdit,
    StopIn,
    StopPatch,
    StopRatingIn,
    VenueIn,
)
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]
LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 24)


# --- couples ---------------------------------------------------------------


async def test_upsert_couple_inserts_then_updates(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session, CoupleProfileIn(name="A & B", home_area="Ventura")
        )
        couple_id = couple.id
    async with db() as session, session.begin():
        again = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple_id, budget_per_date=300)
        )
        assert again.id == couple_id
        # The field it didn't mention is still there.
        assert again.home_area == "Ventura"
        assert again.budget_per_date == 300
        assert len(await repo.list_couples(session)) == 1


async def test_a_new_couple_needs_a_name(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(ValueError, match="needs a name"):
            await repo.upsert_couple(session, CoupleProfileIn(home_area="Ventura"))


async def test_an_explicit_null_clears_an_optional_field(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(
            session, CoupleProfileIn(name="A & B", budget_per_date=300)
        )
        cleared = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple.id, budget_per_date=None)
        )
        assert cleared.budget_per_date is None


async def test_a_null_name_is_ignored_rather_than_breaking_the_row(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        couple = await repo.upsert_couple(session, CoupleProfileIn(name="A & B"))
        same = await repo.upsert_couple(
            session, CoupleProfileIn(id=couple.id, name=None)
        )
        assert same.name == "A & B"


async def _couple_of_two(session: AsyncSession) -> Couple:
    couple = await repo.upsert_couple(session, CoupleProfileIn(name="A & B"))
    await repo.upsert_member(session, couple.id, MemberIn(name="Ann", email="ann@x.io"))
    await repo.upsert_member(session, couple.id, MemberIn(name="Bo"))
    return couple


async def test_upserting_a_member_matches_by_name_then_adds(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        same = await repo.upsert_member(
            session, couple.id, MemberIn(name="ANN", email="new@x.io")
        )
        assert same.email == "new@x.io"
        assert [m.name for m in couple.members] == ["Ann", "Bo"]
        with pytest.raises(ValueError, match="needs a name"):
            await repo.upsert_member(session, couple.id, MemberIn(email="c@x.io"))


async def test_members_are_found_by_name_email_or_id(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        ann = await repo.resolve_member(session, couple.id, " ann ")
        assert (await repo.resolve_member(session, couple.id, "ANN@x.io")) is ann
        assert (await repo.resolve_member(session, couple.id, str(ann.id))) is ann
        with pytest.raises(NotFound, match="members: Ann, Bo"):
            await repo.resolve_member(session, couple.id, "Cy")


async def test_editing_preferences_keeps_what_it_doesnt_name(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        await repo.edit_preferences(
            session, couple.id, "Ann", PreferenceEdit(add_likes=["sushi", "wine bars"])
        )
        edited = await repo.edit_preferences(
            session, couple.id, "Ann", PreferenceEdit(add_likes=["tapas", " Sushi "])
        )
        ann = await repo.resolve_member(session, couple.id, "Ann")
        bo = await repo.resolve_member(session, couple.id, "Bo")
        # Re-adding an item already there, in another case, doesn't duplicate it.
        assert ann.likes == ["sushi", "wine bars", "tapas"]
        assert bo.likes == []
        assert edited.id == couple.id
    async with db() as session:
        stored = await repo.resolve_member(session, couple.id, "Ann")
        assert stored.likes == ["sushi", "wine bars", "tapas"]


async def test_disliking_something_takes_it_off_the_likes(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        await repo.edit_preferences(
            session,
            couple.id,
            "Ann",
            PreferenceEdit(add_likes=["sushi"], add_dislikes=["jazz"]),
        )
        await repo.edit_preferences(
            session,
            couple.id,
            "Ann",
            PreferenceEdit(add_dislikes=["SUSHI"], add_likes=["jazz"]),
        )
        ann = await repo.resolve_member(session, couple.id, "Ann")
        assert ann.likes == ["jazz"]
        assert ann.dislikes == ["SUSHI"]


async def test_dietary_needs_can_be_added_and_dropped(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        await repo.edit_preferences(
            session, couple.id, "Bo", PreferenceEdit(add_dietary=["no shellfish"])
        )
        await repo.edit_preferences(
            session,
            couple.id,
            "Bo",
            PreferenceEdit(remove_dietary=["No Shellfish"], add_dietary=["vegetarian"]),
        )
        bo = await repo.resolve_member(session, couple.id, "Bo")
        assert bo.dietary == ["vegetarian"]


async def test_shared_taste_pools_dislikes_and_drops_vetoed_likes(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        await repo.edit_preferences(
            session,
            couple.id,
            "Ann",
            PreferenceEdit(add_likes=["sushi", "hiking"], add_dietary=["vegan"]),
        )
        await repo.edit_preferences(
            session,
            couple.id,
            "Bo",
            PreferenceEdit(add_likes=["jazz"], add_dislikes=["Sushi"]),
        )
        shared = repo.effective_preferences(couple)
        assert shared.likes == ["hiking", "jazz"]
        assert shared.dislikes == ["Sushi"]
        assert shared.dietary == ["vegan"]
        # A date that's Ann's keeps only her likes, but Bo's dislikes still bind.
        ann = await repo.resolve_member(session, couple.id, "Ann")
        hers = repo.effective_preferences(couple, ann)
        assert hers.likes == ["hiking"]
        assert hers.dislikes == ["Sushi"]


async def test_next_turn_goes_to_whoever_was_planned_for_least_recently(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        first = await repo.next_turn(session, couple)
        # Nobody's had a turn: the member added first.
        assert first is not None and first.name == "Ann"
        assert first.last_planned_for is None

        await repo.create_date(
            session, couple_id=couple.id, title="Hers", day=DAY, planned_for="ann"
        )
        # A date for both doesn't count towards anyone's turn.
        await repo.create_date(session, couple_id=couple.id, title="Both", day=DAY)
        turn = await repo.next_turn(session, couple)
        assert turn is not None and turn.name == "Bo"

        his = await repo.create_date(
            session,
            couple_id=couple.id,
            title="His",
            day=DAY + timedelta(days=7),
            planned_for="Bo",
        )
        turn = await repo.next_turn(session, couple)
        assert turn is not None and turn.name == "Ann"
        assert turn.last_planned_for == DAY

        # A cancelled date never happened, so it hands the turn back.
        await repo.update_date(session, his.id, DatePatch(status="cancelled"))
        turn = await repo.next_turn(session, couple)
        assert turn is not None and turn.name == "Bo"


async def test_a_date_can_be_made_shared_again(db: Factory) -> None:
    async with db() as session, session.begin():
        couple = await _couple_of_two(session)
        planned = await repo.create_date(
            session, couple_id=couple.id, title="Hers", day=DAY, planned_for="Ann"
        )
        assert planned.planned_for == "Ann"
        patched = await repo.update_date(
            session, planned.id, DatePatch(planned_for=None)
        )
        assert patched.planned_for is None
        with pytest.raises(NotFound):
            await repo.update_date(session, planned.id, DatePatch(planned_for="Cy"))


async def test_unknown_couple_is_not_found(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(NotFound):
            await repo.get_couple(session, uuid.uuid4())


# --- venues ----------------------------------------------------------------


def _venue(name: str, city: str, **extra: Any) -> VenueIn:
    return VenueIn(name=name, city=city, venue_type=VenueType.RESTAURANT, **extra)


async def test_venues_are_keyed_on_name_and_city(db: Factory) -> None:
    async with db() as session, session.begin():
        first = await repo.upsert_venue(session, _venue("Nobu Malibu", "Malibu"))
        # Updating an existing venue doesn't need its type again.
        second = await repo.upsert_venue(
            session,
            VenueIn(name="nobu malibu", city="MALIBU", phone="+13103179140"),
        )
        assert second.id == first.id
        assert second.phone == "+13103179140"
        assert second.venue_type is VenueType.RESTAURANT
        assert len(await repo.list_venues(session)) == 1


async def test_find_venue_ignores_case_and_padding(db: Factory) -> None:
    async with db() as session, session.begin():
        await repo.upsert_venue(session, _venue("Bestia", "Los Angeles"))
        assert await repo.find_venue(session, " bestia ", "los angeles")
        assert await repo.find_venue(session, "Bestia", "Malibu") is None


async def test_find_venue_forgives_wording_in_the_same_city(db: Factory) -> None:
    async with db() as session, session.begin():
        nobu = await repo.upsert_venue(session, _venue("Nobu Malibu", "Malibu"))
        for asked in ("Nobu", "NOBU Malibu Restaurant", "The Nobu Malibu"):
            found = await repo.find_venue(session, asked, "malibu")
            assert found is not None and found.id == nobu.id
        # A different venue sharing no words is not it.
        assert await repo.find_venue(session, "Bavel", "Malibu") is None
        assert await repo.find_venue(session, "Nobu", "Los Angeles") is None


async def test_find_venue_crosses_cities_only_by_coordinates(db: Factory) -> None:
    async with db() as session, session.begin():
        getty = await repo.upsert_venue(
            session,
            VenueIn(
                name="Getty Villa",
                city="Pacific Palisades",
                venue_type=VenueType.MUSEUM,
                lat=34.0449819,
                lng=-118.5650197,
            ),
        )
        found = await repo.find_venue(
            session, "Getty Villa", "Malibu", 34.0452, -118.5651
        )
        assert found is not None and found.id == getty.id
        assert await repo.find_venue(session, "Getty Villa", "Malibu") is None
        far = await repo.find_venue(session, "Getty Villa", "Malibu", 34.2, -118.9)
        assert far is None


async def test_chain_branches_in_one_city_are_told_apart_by_coordinates(
    db: Factory,
) -> None:
    arts = (34.0407, -118.2336)
    venice = (33.9915, -118.4695)
    async with db() as session, session.begin():
        for name, (lat, lng) in (
            ("Blue Bottle Coffee Arts District", arts),
            ("Blue Bottle Coffee Venice", venice),
        ):
            await repo.upsert_venue(
                session,
                VenueIn(
                    name=name,
                    city="Los Angeles",
                    venue_type=VenueType.RESTAURANT,
                    lat=lat,
                    lng=lng,
                ),
            )
        la = "Los Angeles"
        found = await repo.find_venue(session, "Blue Bottle Coffee", la, *venice)
        assert found is not None and found.name.endswith("Venice")
        # Pinned somewhere else: neither branch, so a new venue, not a guess.
        assert (
            await repo.find_venue(session, "Blue Bottle Coffee", la, 34.1, -118.3)
            is None
        )
        # No pin: both fit, so it asks.
        with pytest.raises(AmbiguousVenue, match="Venice"):
            await repo.find_venue(session, "Blue Bottle Coffee", la)
        with pytest.raises(AmbiguousVenue):
            await repo.upsert_venue(
                session, VenueIn(name="Blue Bottle Coffee", city=la, notes="x")
            )


async def test_upserting_a_reworded_venue_updates_it_and_keeps_its_name(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        first = await repo.upsert_venue(session, _venue("Nobu Malibu", "Malibu"))
        again = await repo.upsert_venue(
            session, VenueIn(name="Nobu", city="Malibu", notes="Ask for the bar")
        )
        assert again.id == first.id
        assert (again.name, again.notes) == ("Nobu Malibu", "Ask for the bar")
        assert len(await repo.list_venues(session)) == 1


PLACE = {
    "id": "ChIJ-nobu",
    "displayName": {"text": "Nobu Malibu"},
    "formattedAddress": "22706 Pacific Coast Hwy, Malibu, CA 90265, USA",
    "location": {"latitude": 34.039, "longitude": -118.6696},
}


async def test_apply_place_fills_gaps_and_refuses_a_place_held_elsewhere(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        nobu = await repo.upsert_venue(session, _venue("Nobu Malibu", "Malibu"))
        other = await repo.upsert_venue(session, _venue("Matsuhisa", "Beverly Hills"))
        assert await repo.apply_place(session, nobu, PLACE) == [
            "lat",
            "lng",
            "address",
        ]
        assert nobu.google_place_data == PLACE
        with pytest.raises(DuplicateVenue, match="Nobu Malibu"):
            await repo.apply_place(session, other, PLACE)
        with pytest.raises(DuplicateVenue):
            await repo.upsert_venue(
                session, VenueIn(id=other.id, google_place_id="ChIJ-nobu")
            )
        # Re-applying to the venue that holds it is fine.
        await repo.apply_place(session, nobu, PLACE)


async def test_a_new_venue_needs_a_name_city_and_type(db: Factory) -> None:
    async with db() as session, session.begin():
        with pytest.raises(ValueError, match="name and a city"):
            await repo.upsert_venue(session, _venue("Bestia", ""))
        with pytest.raises(ValueError, match="venue_type"):
            await repo.upsert_venue(session, VenueIn(name="Bestia", city="LA"))


async def test_a_venue_keeps_where_it_is(db: Factory) -> None:
    async with db() as session, session.begin():
        trail = await repo.upsert_venue(
            session,
            VenueIn(
                name="Solstice Canyon",
                city="Malibu",
                venue_type=VenueType.TRAIL,
                lat=34.03765,
                lng=-118.7477,
                external_url="https://www.alltrails.com/trail/x",
                default_duration_min=105,
            ),
        )
        assert trail.has_coordinates
        assert trail.default_duration_min == 105


async def test_record_detection_caches_channel_and_window(db: Factory) -> None:
    async with db() as session, session.begin():
        venue = await repo.upsert_venue(session, _venue("Bestia", "Los Angeles"))
        await repo.record_detection(
            session,
            venue,
            Detection(
                channel=BookingChannel.RESY,
                confidence=0.95,
                evidence="resy link",
                platform_slug="bestia",
                platform_city_slug="los-angeles-ca",
                phone="+12135550000",
                window=BookingWindow(30, WindowUnit.DAYS),
            ),
        )
        assert venue.booking_channel is BookingChannel.RESY
        assert venue.platform_slug == "bestia"
        assert venue.channel_checked_at is not None
        assert venue.window_amount == 30
        assert venue.window_unit is WindowUnit.DAYS
        assert venue.phone == "+12135550000"


async def test_detection_does_not_overwrite_what_a_person_entered(
    db: Factory,
) -> None:
    async with db() as session, session.begin():
        venue = await repo.upsert_venue(
            session,
            _venue(
                "Nobu Malibu",
                "Malibu",
                phone="+13103179140",
                window_amount=1,
                window_unit=WindowUnit.MONTHS,
            ),
        )
        await repo.record_detection(
            session,
            venue,
            Detection(
                channel=BookingChannel.PHONE,
                confidence=0.8,
                evidence="calls only",
                phone="+10000000000",
                window=BookingWindow(30, WindowUnit.DAYS),
            ),
        )
        assert venue.phone == "+13103179140"
        assert venue.window_amount == 1
        assert venue.window_unit is WindowUnit.MONTHS


# --- dates and stops -------------------------------------------------------


def _stop(title: str, **extra: Any) -> StopIn:
    values: dict[str, Any] = {"kind": StopKind.ACTIVITY} | extra
    return StopIn(title=title, **values)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute), LA)


async def _date(session: AsyncSession, couple_id: uuid.UUID) -> uuid.UUID:
    planned = await repo.create_date(
        session, couple_id=couple_id, title="Malibu day", day=DAY
    )
    return planned.id


async def test_a_new_date_is_planning_with_no_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        planned = await repo.create_date(
            session, couple_id=seeded["couple_id"], title="Malibu day", day=DAY
        )
        assert planned.status is DateStatus.PLANNING
        assert planned.max_leg_minutes == 25
        assert planned.stops == []


async def test_create_date_checks_the_couple_and_window(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        with pytest.raises(NotFound):
            await repo.create_date(
                session, couple_id=uuid.uuid4(), title="Nobody's date", day=DAY
            )
    async with db() as session, session.begin():
        with pytest.raises(ValueError, match="end_before"):
            await repo.create_date(
                session,
                couple_id=seeded["couple_id"],
                title="Backwards",
                day=DAY,
                start_after=time(18, 0),
                end_before=time(9, 0),
            )


async def test_stops_keep_their_order(db: Factory, seeded: dict[str, Any]) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        await repo.add_stop(session, date_id, _stop("Hike"))
        await repo.add_stop(session, date_id, _stop("Dinner", kind=StopKind.MEAL))
        planned = await repo.add_stop(session, date_id, _stop("Museum"), position=1)
        assert [s.title for s in planned.stops] == ["Hike", "Museum", "Dinner"]
        assert [s.position for s in planned.stops] == [0, 1, 2]

        hike, museum, dinner = planned.stops
        swapped = await repo.reorder_stops(
            session, date_id, [museum.id, hike.id, dinner.id]
        )
        assert [s.title for s in swapped.stops] == ["Museum", "Hike", "Dinner"]

        trimmed = await repo.remove_stop(session, hike.id)
        assert [(s.title, s.position) for s in trimmed.stops] == [
            ("Museum", 0),
            ("Dinner", 1),
        ]
    # The deferred unique (date_id, position) held at commit.
    async with db() as session:
        stored = await repo.get_date(session, date_id)
        assert [s.title for s in stored.stops] == ["Museum", "Dinner"]


async def test_reorder_needs_every_stop_once(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        await repo.add_stop(session, date_id, _stop("Hike"))
        planned = await repo.add_stop(session, date_id, _stop("Dinner"))
        first = planned.stops[0].id
        with pytest.raises(ValueError, match="exactly once"):
            await repo.reorder_stops(session, date_id, [first, first])
        with pytest.raises(ValueError, match="exactly once"):
            await repo.reorder_stops(session, date_id, [first])


async def test_a_stop_needs_an_existing_venue_and_sane_times(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        with pytest.raises(NotFound):
            await repo.add_stop(
                session, date_id, _stop("Nowhere", venue_id=uuid.uuid4())
            )
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        with pytest.raises(ValueError, match="after start_at"):
            await repo.add_stop(
                session, date_id, _stop("Backwards", start_at=_at(19), end_at=_at(18))
            )


async def test_booking_status_defaults_from_the_need(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        for need in ReservationNeed:
            await repo.add_stop(session, date_id, _stop(need, reservation_need=need))
        planned = await repo.get_date(session, date_id)
        statuses = {s.reservation_need: s.booking_status for s in planned.stops}
        assert statuses == {
            ReservationNeed.NONE: StopBookingStatus.NOT_NEEDED,
            ReservationNeed.REQUIRED: StopBookingStatus.NEEDED,
            ReservationNeed.RECOMMENDED: StopBookingStatus.NEEDED,
            # Unknown waits: nobody has looked yet.
            ReservationNeed.UNKNOWN: StopBookingStatus.NEEDED,
        }


async def test_the_date_status_follows_its_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    """planning -> booking_pending -> confirmed, purely from stop changes."""
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        planned = await repo.add_stop(
            session,
            date_id,
            _stop("Hike", reservation_need=ReservationNeed.NONE, est_duration_min=90),
        )
        # Nothing to book, but no time yet.
        assert planned.status is DateStatus.PLANNING
        hike = planned.stops[0]

        planned = await repo.add_stop(
            session,
            date_id,
            _stop(
                "Dinner",
                kind=StopKind.MEAL,
                venue_id=seeded["nobu_id"],
                reservation_need=ReservationNeed.RECOMMENDED,
            ),
        )
        assert planned.status is DateStatus.BOOKING_PENDING
        dinner = planned.stops[1]

        for status in (StopBookingStatus.LINK_SENT, StopBookingStatus.REMINDER_SET):
            planned = await repo.update_stop(
                session, dinner.id, StopPatch(booking_status=status)
            )
            assert planned.status is DateStatus.BOOKING_PENDING

        planned = await repo.update_stop(
            session,
            dinner.id,
            StopPatch(
                booking_status=StopBookingStatus.CONFIRMED,
                confirmation_number="NOBU-1",
                start_at=_at(17, 45),
                end_at=_at(19, 15),
            ),
        )
        # Booked, but the hike still has no time.
        assert planned.status is DateStatus.PLANNING

        planned = await repo.update_stop(
            session, hike.id, StopPatch(start_at=_at(9), end_at=_at(10, 30))
        )
        assert planned.status is DateStatus.CONFIRMED

        # Clearing a time takes it back.
        planned = await repo.update_stop(session, hike.id, StopPatch(end_at=None))
        assert planned.status is DateStatus.PLANNING


async def test_a_cancelled_stop_counts_for_nothing(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        planned = await repo.add_stop(
            session,
            date_id,
            _stop(
                "Hike",
                reservation_need=ReservationNeed.NONE,
                start_at=_at(9),
                end_at=_at(10),
            ),
        )
        assert planned.status is DateStatus.CONFIRMED
        planned = await repo.add_stop(
            session,
            date_id,
            _stop("Dinner", reservation_need=ReservationNeed.REQUIRED),
        )
        assert planned.status is DateStatus.BOOKING_PENDING
        planned = await repo.update_stop(
            session,
            planned.stops[1].id,
            StopPatch(booking_status=StopBookingStatus.CANCELLED),
        )
        assert planned.status is DateStatus.CONFIRMED


async def test_a_new_need_moves_a_defaulted_booking_status(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        planned = await repo.add_stop(session, date_id, _stop("Hike"))
        hike = planned.stops[0]
        assert hike.booking_status is StopBookingStatus.NEEDED  # unknown waits
        planned = await repo.update_stop(
            session, hike.id, StopPatch(reservation_need=ReservationNeed.NONE)
        )
        hike = planned.stops[0]
        assert hike.booking_status is StopBookingStatus.NOT_NEEDED

        # A status somebody set isn't a default, so it stays.
        planned = await repo.add_stop(
            session, date_id, _stop("Dinner", reservation_need=ReservationNeed.REQUIRED)
        )
        dinner = planned.stops[1]
        await repo.update_stop(
            session, dinner.id, StopPatch(booking_status=StopBookingStatus.LINK_SENT)
        )
        await repo.update_stop(
            session, dinner.id, StopPatch(reservation_need=ReservationNeed.NONE)
        )
        assert dinner.booking_status is StopBookingStatus.LINK_SENT


async def test_a_new_day_takes_the_stops_with_it(
    db: Factory, seeded: dict[str, Any]
) -> None:
    """Wall-clock times carry over, across the DST change on Nov 1; a
    booking made for the old day goes back to `needed`."""
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        await repo.add_stop(
            session,
            date_id,
            _stop(
                "Hike",
                reservation_need=ReservationNeed.NONE,
                start_at=_at(9),
                end_at=_at(10, 30),
            ),
        )
        planned = await repo.add_stop(
            session,
            date_id,
            _stop(
                "Dinner",
                kind=StopKind.MEAL,
                reservation_need=ReservationNeed.REQUIRED,
                booking_status=StopBookingStatus.CONFIRMED,
                booking_url="https://resy.com/cities/la/venues/nobu?date=2026-10-24",
                confirmation_number="NOBU-1",
                start_at=_at(19),
                end_at=_at(20, 30),
            ),
        )
        assert planned.status is DateStatus.CONFIRMED

        moved = date(2026, 11, 7)
        planned = await repo.update_date(session, date_id, DatePatch(day=moved))
        hike, dinner = planned.stops
        assert hike.start_at == datetime.combine(moved, time(9), LA)
        assert hike.end_at == datetime.combine(moved, time(10, 30), LA)
        assert dinner.start_at == datetime.combine(moved, time(19), LA)
        assert hike.booking_status is StopBookingStatus.NOT_NEEDED
        assert dinner.booking_status is StopBookingStatus.NEEDED
        assert dinner.booking_url is None
        assert dinner.confirmation_number == "NOBU-1"
        assert planned.status is DateStatus.BOOKING_PENDING


async def test_the_same_day_moves_nothing(db: Factory, seeded: dict[str, Any]) -> None:
    async with db() as session, session.begin():
        date_id = await _date(session, seeded["couple_id"])
        await repo.add_stop(
            session,
            date_id,
            _stop(
                "Dinner",
                reservation_need=ReservationNeed.REQUIRED,
                booking_status=StopBookingStatus.CONFIRMED,
                start_at=_at(19),
                end_at=_at(20),
            ),
        )
        planned = await repo.update_date(
            session, date_id, DatePatch(day=DAY, notes="same day")
        )
        assert planned.stops[0].start_at == _at(19)
        assert planned.stops[0].booking_status is StopBookingStatus.CONFIRMED


async def test_a_stop_change_costs_the_same_queries_however_many_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async def queries_to_rename(stops: int) -> int:
        async with db() as session, session.begin():
            date_id = await _date(session, seeded["couple_id"])
            for n in range(stops):
                planned = await repo.add_stop(
                    session, date_id, _stop(f"Stop {n}", venue_id=seeded["nobu_id"])
                )
            statements: list[str] = []
            engine = session.bind.sync_engine

            def count(*args: Any) -> None:
                statements.append(args[2])

            event.listen(engine, "before_cursor_execute", count)
            try:
                await repo.update_stop(
                    session, planned.stops[0].id, StopPatch(title="Renamed")
                )
            finally:
                event.remove(engine, "before_cursor_execute", count)
            return len(statements)

    assert await queries_to_rename(2) == await queries_to_rename(6)


async def test_completed_and_cancelled_stick(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        loved = await repo.get_date(session, seeded["loved_date_id"])
        assert loved.status is DateStatus.COMPLETED
        assert [r.score for r in loved.ratings] == [10]
        # A stop change doesn't un-complete it...
        await repo.update_stop(
            session,
            loved.stops[0].id,
            StopPatch(booking_status=StopBookingStatus.NEEDED),
        )
        assert loved.status is DateStatus.COMPLETED
        # ...and nor can a patch.
        with pytest.raises(InvalidTransition):
            await repo.update_date(session, loved.id, DatePatch(status="cancelled"))
        same = await repo.update_date(
            session, loved.id, DatePatch(status="completed", notes="still great")
        )
        assert same.notes == "still great"


def test_only_hand_set_statuses_can_be_patched() -> None:
    with pytest.raises(ValidationError):
        DatePatch(status="confirmed")  # type: ignore[arg-type]


async def test_list_past_dates_is_newest_first_and_skips_cancelled(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        past = await repo.list_past_dates(session, seeded["couple_id"])
        assert [p.id for p in past] == [seeded["meh_date_id"], seeded["loved_date_id"]]
        assert [[r.score for r in p.ratings] for p in past] == [[6], [10]]
        # Every stop's venue comes along, for spotting repeats.
        assert [p.stops[0].venue_name for p in past] == ["Bestia", "Nobu Malibu"]

        today = datetime.now(LA).date()
        await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Upcoming",
            day=today + timedelta(days=3),
        )
        assert len(await repo.list_past_dates(session, seeded["couple_id"])) == 2

        flaked = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Cancelled on us",
            day=today - timedelta(days=2),
        )
        await repo.update_date(session, flaked.id, DatePatch(status="cancelled"))
        after = await repo.list_past_dates(session, seeded["couple_id"])
        assert [p.id for p in after] == [seeded["meh_date_id"], seeded["loved_date_id"]]


async def test_list_dates_filters_by_status_and_day(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        today = datetime.now(LA).date()
        completed = await repo.list_dates(
            session, seeded["couple_id"], status=DateStatus.COMPLETED
        )
        assert len(completed) == 2
        recent = await repo.list_dates(
            session, seeded["couple_id"], day_from=today - timedelta(days=20)
        )
        assert [r.id for r in recent] == [seeded["meh_date_id"]]
        nothing = await repo.list_dates(
            session, seeded["couple_id"], day_to=today - timedelta(days=365)
        )
        assert nothing == []


# --- travel cache ----------------------------------------------------------


async def test_the_travel_cache_hits_misses_and_expires(
    db: Factory, seeded: dict[str, Any]
) -> None:
    a, b = seeded["getty_id"], seeded["nobu_id"]
    driving = TravelMode.DRIVING
    async with db() as session, session.begin():
        assert (
            await repo.cached_travel(session, a, b, driving, "haversine", ttl_days=30)
            is None
        )
        await repo.cache_travel(
            session, a, b, driving, "haversine", minutes=12.5, meters=9000
        )
        hit = await repo.cached_travel(session, a, b, driving, "haversine", ttl_days=30)
        assert hit is not None and hit.minutes == 12.5
        # Directional, and per mode and provider.
        for key in (
            (b, a, driving, "haversine"),
            (a, b, TravelMode.WALKING, "haversine"),
            (a, b, driving, "osrm"),
        ):
            assert await repo.cached_travel(session, *key, ttl_days=30) is None
        # Expired once it's ttl_days old.
        later = datetime.now(timezone.utc) + timedelta(days=30)
        expired = await repo.cached_travel(
            session, a, b, driving, "haversine", ttl_days=30, now=later
        )
        assert expired is None
        # A recompute replaces the row rather than colliding with it.
        await repo.cache_travel(
            session, a, b, driving, "haversine", minutes=14.0, meters=9100
        )
        fresh = await repo.cached_travel(
            session, a, b, driving, "haversine", ttl_days=30
        )
        assert fresh is not None and fresh.minutes == 14.0


# --- ratings ---------------------------------------------------------------


async def _two_stop_date(session: AsyncSession, seeded: dict[str, Any]) -> Any:
    """Yesterday's hike then dinner, not yet rated."""
    day = datetime.now(LA).date() - timedelta(days=1)
    planned = await repo.create_date(
        session, couple_id=seeded["couple_id"], title="Hike and dinner", day=day
    )
    for title, kind, venue in (
        ("Hike", StopKind.ACTIVITY, seeded["solstice_id"]),
        ("Dinner", StopKind.MEAL, seeded["nobu_id"]),
    ):
        await repo.add_stop(
            session, planned.id, StopIn(title=title, kind=kind, venue_id=venue)
        )
    return planned


def _scores(ratings: list[Any]) -> dict[str, tuple[int, str | None]]:
    return {r.member_name: (r.score, r.notes) for r in ratings}


async def test_each_member_rates_the_date_and_its_stops(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        planned = await _two_stop_date(session, seeded)
        hike, dinner = planned.stops
        await repo.rate_date(
            session,
            planned.id,
            "Alex",
            score=8,
            notes="Great views",
            stops=[
                StopRatingIn(stop_id=hike.id, score=10, notes="Waterfall!"),
                StopRatingIn(stop_id=dinner.id, score=0),
            ],
        )
        rated = await repo.rate_date(
            session,
            planned.id,
            "b@example.com",
            score=6,
            stops=[StopRatingIn(stop_id=dinner.id, score=7)],
            source=RatingSource.PREVIEW,
        )
        assert rated.status is DateStatus.COMPLETED
        assert _scores(rated.ratings) == {
            "Alex": (8, "Great views"),
            "Sam": (6, None),
        }
        assert _scores(rated.stops[0].ratings) == {"Alex": (10, "Waterfall!")}
        assert _scores(rated.stops[1].ratings) == {"Alex": (0, None), "Sam": (7, None)}
        assert {r.source for r in rated.stops[1].ratings} == {
            RatingSource.CHAT,
            RatingSource.PREVIEW,
        }

    # A second call changes only what it names; the rest stays.
    async with db() as session, session.begin():
        changed = await repo.rate_date(
            session,
            planned.id,
            "Alex",
            notes="Great views, long drive",
            stops=[StopRatingIn(stop_id=dinner.id, score=4)],
        )
        assert _scores(changed.ratings)["Alex"] == (8, "Great views, long drive")
        assert _scores(changed.stops[1].ratings)["Alex"] == (4, None)
        assert len(changed.ratings) == 2

    async with db() as session, session.begin():
        past = await repo.list_past_dates(session, seeded["couple_id"])
        mine = next(p for p in past if p.id == planned.id)
        assert _scores(mine.stops[0].ratings) == {"Alex": (10, "Waterfall!")}


async def test_a_first_rating_needs_a_score(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        planned = await _two_stop_date(session, seeded)
        with pytest.raises(IncompleteRating):
            await repo.rate_date(session, planned.id, "Alex", notes="Fun")
        with pytest.raises(IncompleteRating):
            await repo.rate_date(
                session,
                planned.id,
                "Alex",
                stops=[StopRatingIn(stop_id=planned.stops[0].id, notes="Steep")],
            )
        with pytest.raises(IncompleteRating):
            await repo.rate_date(session, planned.id, "Alex")


async def test_only_this_dates_stops_can_be_rated(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        planned = await _two_stop_date(session, seeded)
        other = await repo.get_date(session, seeded["loved_date_id"])
        with pytest.raises(NotFound, match="Hike = "):
            await repo.rate_date(
                session,
                planned.id,
                "Alex",
                stops=[StopRatingIn(stop_id=other.stops[0].id, score=5)],
            )
        with pytest.raises(NotFound, match="members: Alex, Sam"):
            await repo.rate_date(session, planned.id, "Cy", score=5)


async def test_future_and_cancelled_dates_cant_be_rated(
    db: Factory, seeded: dict[str, Any]
) -> None:
    async with db() as session, session.begin():
        today = datetime.now(LA).date()
        upcoming = await repo.create_date(
            session,
            couple_id=seeded["couple_id"],
            title="Soon",
            day=today + timedelta(days=2),
        )
        with pytest.raises(InvalidTransition, match="once it has happened"):
            await repo.rate_date(session, upcoming.id, "Alex", score=5)
        # Today counts: the evening's date can be rated that night.
        tonight = await repo.create_date(
            session, couple_id=seeded["couple_id"], title="Tonight", day=today
        )
        assert (await repo.rate_date(session, tonight.id, "Alex", score=5)).ratings
        off = await repo.create_date(
            session, couple_id=seeded["couple_id"], title="Off", day=today
        )
        await repo.update_date(session, off.id, DatePatch(status="cancelled"))
        with pytest.raises(InvalidTransition, match="cancelled"):
            await repo.rate_date(session, off.id, "Alex", score=5)


def test_scores_run_zero_to_ten() -> None:
    StopRatingIn(stop_id=uuid.uuid4(), score=0)
    StopRatingIn(stop_id=uuid.uuid4(), score=10)
    with pytest.raises(ValidationError):
        StopRatingIn(stop_id=uuid.uuid4(), score=11)
    with pytest.raises(ValidationError):
        StopRatingIn(stop_id=uuid.uuid4(), score=-1)
    with pytest.raises(ValidationError):
        DatePatch(rating=5)  # type: ignore[call-arg]


async def test_a_rating_can_be_taken_back(db: Factory, seeded: dict[str, Any]) -> None:
    async with db() as session, session.begin():
        planned = await _two_stop_date(session, seeded)
        hike = planned.stops[0]
        await repo.rate_date(
            session,
            planned.id,
            "Alex",
            score=7,
            stops=[StopRatingIn(stop_id=hike.id, score=9)],
        )
        cleared = await repo.clear_rating(session, planned.id, "Alex", hike.id)
        assert cleared.stops[0].ratings == []
        assert _scores(cleared.ratings) == {"Alex": (7, None)}
        cleared = await repo.clear_rating(session, planned.id, "Alex")
        assert cleared.ratings == []
        with pytest.raises(NotFound, match="hasn't rated"):
            await repo.clear_rating(session, planned.id, "Alex")
