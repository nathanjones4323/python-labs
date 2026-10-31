from datetime import UTC, date, datetime, timedelta

import pytest
from campsite_booker import service
from campsite_booker.availability import StayCriteria
from campsite_booker.checkout import CartItem, CheckoutResult
from campsite_booker.db import (
    CHALLENGED,
    DRY_RUN,
    EXPIRED,
    FAILED,
    HELD,
    PAID,
    Opening,
    Watch,
)
from campsite_booker.recgov import Campground
from conftest import site
from sqlalchemy import select

NOW = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
TODAY = date(2026, 10, 27)
PINES = Campground(id="232447", name="Upper Pines")
FRI = date(2026, 11, 6)


def watch(db, **kw) -> Watch:
    crit = StayCriteria(
        arrival_from=kw.pop("arrival_from", FRI),
        arrival_to=kw.pop("arrival_to", FRI),
        nights=kw.pop("nights", 2),
        **kw.pop("criteria", {}),
    )
    return service.create_watch(db, kw.pop("campground", PINES), crit, **kw)


def check(db, recgov, now=NOW, today=TODAY):
    return service.check_watches(db, recgov, now, today)


def test_a_new_opening_alerts_once(db, recgov):
    w = watch(db)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)], site="044")]

    alerts = check(db, recgov)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.watch_id == w.id
    assert alert.campground_name == "Upper Pines"
    assert [(s.stay.site, s.stay.check_in) for s in alert.stays] == [("044", FRI)]
    assert alert.auto_hold is False
    # Still free next check: nothing new.
    assert check(db, recgov, NOW + timedelta(minutes=1)) == []
    # Fetched once, for exactly the nights the watch needs.
    assert recgov.calls[0] == (PINES.id, FRI, FRI + timedelta(days=1))


def test_an_opening_that_goes_and_comes_back_alerts_again(db, recgov):
    watch(db)
    free = [site("100", [FRI, FRI + timedelta(days=1)])]
    recgov.sites[PINES.id] = free
    first = check(db, recgov)[0].stays[0].opening_id
    service.mark_notified(db, [first], NOW)

    recgov.sites[PINES.id] = [site("100", [])]
    assert check(db, recgov, NOW + timedelta(minutes=1)) == []
    with db() as session:
        assert session.get(Opening, first).gone_at is not None

    recgov.sites[PINES.id] = free
    again = check(db, recgov, NOW + timedelta(minutes=2))
    assert [s.opening_id for s in again[0].stays] == [first]
    with db() as session:
        opening = session.get(Opening, first)
        assert opening.gone_at is None and opening.notified_at is None


def test_one_fetch_per_campground_covering_every_watch(db, recgov):
    watch(db, arrival_from=FRI, arrival_to=FRI, nights=2)
    watch(db, arrival_from=date(2026, 11, 20), arrival_to=date(2026, 11, 27), nights=3)
    check(db, recgov)
    assert recgov.calls == [(PINES.id, FRI, date(2026, 11, 29))]


def test_arrivals_already_past_are_not_fetched_and_ended_watches_expire(db, recgov):
    w = watch(db, arrival_from=date(2026, 10, 20), arrival_to=date(2026, 10, 30))
    old = watch(db, arrival_from=date(2026, 10, 1), arrival_to=date(2026, 10, 2))
    check(db, recgov)
    assert recgov.calls == [(PINES.id, TODAY, date(2026, 10, 31))]
    with db() as session:
        assert session.get(Watch, old.id).active is False
        assert session.get(Watch, old.id).ended_reason == EXPIRED
        assert session.get(Watch, w.id).active is True


def test_a_failing_campground_records_the_error_and_spares_the_rest(db, recgov):
    broken = watch(db, campground=Campground(id="9", name="Broken"))
    fine = watch(db)
    recgov.fail.add("9")
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    alerts = check(db, recgov)
    assert [a.watch_id for a in alerts] == [fine.id]
    with db() as session:
        assert "503" in session.get(Watch, broken.id).last_error
        assert session.get(Watch, fine.id).last_checked_at == NOW


def test_stopped_watches_are_not_checked(db, recgov):
    w = watch(db)
    service.stop_watch(db, w.id)
    check(db, recgov)
    assert recgov.calls == []
    service.resume_watch(db, w.id)
    check(db, recgov)
    assert len(recgov.calls) == 1


def test_unsent_alerts_hold_what_was_found_but_not_delivered(db, recgov):
    w = watch(db)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    check(db, recgov)
    pending = service.unsent_alerts(db)
    assert [(a.watch_id, len(a.stays)) for a in pending] == [(w.id, 1)]
    service.mark_notified(db, [pending[0].stays[0].opening_id], NOW)
    assert service.unsent_alerts(db) == []


def test_auto_hold_is_offered_once(db, recgov):
    w = watch(db, auto_hold=True)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    assert check(db, recgov)[0].auto_hold is True
    assert service.claim_auto_hold(db, w.id, NOW) is True
    assert service.claim_auto_hold(db, w.id, NOW) is False

    # A later opening on the same watch only alerts.
    recgov.sites[PINES.id] = [
        site("100", [FRI, FRI + timedelta(days=1)]),
        site("101", [FRI, FRI + timedelta(days=1)]),
    ]
    later = check(db, recgov, NOW + timedelta(minutes=1))
    assert later[0].auto_hold is False


def test_record_hold_sets_the_cart_expiry_and_hold_target_round_trips(db, recgov):
    w = watch(db)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)], site="044")]
    opening_id = check(db, recgov)[0].stays[0].opening_id
    target = service.hold_target(db, opening_id)
    assert (target.watch_id, target.stay.site, target.stay.check_out) == (
        w.id,
        "044",
        FRI + timedelta(days=2),
    )
    hold = service.record_hold(db, target, HELD, "ok", "https://x", NOW)
    assert hold.expires_at == NOW + timedelta(minutes=15)
    failed = service.record_hold(db, target, "failed", "gone", None, NOW)
    assert failed.expires_at is None
    assert [h.id for h in service.list_holds(db)] == [failed.id, hold.id]


def test_default_name(db):
    w = watch(db, arrival_to=date(2026, 11, 27))
    assert w.name == "Upper Pines, Nov 6–Nov 27, 2 nights"
    with db() as session:
        assert session.scalars(select(Watch)).one().campsite_ids == []


def test_auto_pay_turns_auto_hold_on_and_rides_on_the_alert(db, recgov):
    w = watch(db, criteria={"group_size": 4})
    service.set_auto_pay(db, w.id, True)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    alert = check(db, recgov)[0]
    assert (alert.auto_hold, alert.auto_pay, alert.group_size) == (True, True, 4)


def test_checkout_outcomes_are_recorded(db, recgov):
    w = watch(db, criteria={"group_size": 3})
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    target = service.hold_target(db, check(db, recgov)[0].stays[0].opening_id)
    dry = CheckoutResult(DRY_RUN, "Dry run")
    hold = service.record_hold(db, target, HELD, "ok", "u", NOW, checkout=dry)
    assert (hold.checkout_status, hold.expires_at) == (
        DRY_RUN,
        NOW + timedelta(minutes=15),
    )
    paid = service.record_checkout(
        db, hold.id, CheckoutResult(PAID, "Paid", "0123-4567")
    )
    assert (paid.checkout_status, paid.confirmation, paid.expires_at) == (
        PAID,
        "0123-4567",
        None,
    )
    assert service.hold_group_size(db, paid, 2) == 3
    assert w.id == paid.watch_id


def test_a_challenged_hold_can_be_paid_once_its_in_the_cart(db, recgov):
    watch(db)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)], site="044")]
    target = service.hold_target(db, check(db, recgov)[0].stays[0].opening_id)
    hold = service.record_hold(db, target, CHALLENGED, "human check", "u", NOW)
    # Not in the cart: no expiry, and not a live hold to skip.
    assert hold.expires_at is None
    assert service.live_hold(db, target.stay, NOW) is None
    assert service.pay_refusal(hold) is None
    assert service.cart_item(hold) == CartItem("044", FRI)

    # Paying found it missing from the cart: still challenged.
    missing = CheckoutResult(FAILED, "Site 044 isn't in your cart")
    assert service.record_checkout(db, hold.id, missing, NOW).status == CHALLENGED

    # You added it: once checkout gets past the cart it's held.
    later = NOW + timedelta(minutes=3)
    dry = service.record_checkout(db, hold.id, CheckoutResult(DRY_RUN, "Dry"), later)
    assert (dry.status, dry.checkout_status) == (HELD, DRY_RUN)
    assert dry.expires_at == later + timedelta(minutes=15)


def test_a_failed_hold_isnt_paid_for_from_the_cart(db, recgov):
    watch(db)
    recgov.sites[PINES.id] = [site("100", [FRI, FRI + timedelta(days=1)])]
    target = service.hold_target(db, check(db, recgov)[0].stays[0].opening_id)
    hold = service.record_hold(db, target, FAILED, "gone", None, NOW)
    assert "nothing to pay" in service.pay_refusal(hold)


def test_check_campgrounds_only_fetches_the_ones_asked_for(db, recgov):
    watch(db)
    watch(db, campground=Campground(id="9", name="Other"))
    result = service.check_campgrounds(db, recgov, NOW, TODAY, {"9"})
    assert [c[0] for c in recgov.calls] == ["9"]
    assert set(result.sites) == {"9"}


def test_watches_made_before_areas_look_theirs_up_on_a_regular_poll(db, recgov):
    w = watch(db)
    with db.begin() as session:
        session.get(Watch, w.id).area = None
    looked_up = []

    def campground(campground_id):
        looked_up.append(campground_id)
        return Campground(id=campground_id, name="Upper Pines", city="YOSEMITE VALLEY")

    recgov.campground = campground
    # A release burst doesn't stop for it.
    service.check_campgrounds(db, recgov, NOW, TODAY, {PINES.id})
    assert looked_up == []
    check(db, recgov)
    check(db, recgov)
    assert looked_up == [PINES.id]
    with db() as session:
        assert session.get(Watch, w.id).area == "Yosemite Valley"


PINES_RELEASES = {
    "current_release": {"end": "2027-03-14T00:00:00Z"},
    "next_release": {
        "release_time": "2026-11-15T10:00:00-05:00",
        "end": "2027-04-14T00:00:00Z",
    },
}


def test_watches_waiting_on_a_release_are_found_and_cached(db, recgov):
    soon = watch(db)  # Nov 6: on sale now
    later = watch(db, arrival_from=date(2027, 3, 19), arrival_to=date(2027, 3, 19))
    recgov.release_payloads[PINES.id] = PINES_RELEASES
    cache = {}
    waiting = service.waiting_on_releases(db, recgov, NOW, TODAY, cache)
    assert [(w.watch_id, w.announced) for w in waiting] == [(later.id, False)]
    assert waiting[0].release_at == datetime(2026, 11, 15, 15, 0, tzinfo=UTC)
    assert soon.id not in [w.watch_id for w in waiting]
    # Both watches share the campground's one fetch, and it's cached.
    assert recgov.release_calls == [PINES.id]
    service.mark_announced(db, [later.id], waiting[0].release_at)
    again = service.waiting_on_releases(
        db, recgov, NOW + timedelta(hours=1), TODAY, cache
    )
    assert [w.announced for w in again] == [True]
    assert recgov.release_calls == [PINES.id]
    # Stale after six hours.
    service.waiting_on_releases(db, recgov, NOW + timedelta(hours=7), TODAY, cache)
    assert recgov.release_calls == [PINES.id, PINES.id]


BIG_SUR = Campground(id="rc-1001-2001", name="Pfeiffer Big Sur Campground")


def test_a_state_park_watch_alerts_but_cant_auto_hold(db, recgov):
    with pytest.raises(ValueError, match="ReserveCalifornia"):
        watch(db, campground=BIG_SUR, auto_hold=True)
    w = watch(db, campground=BIG_SUR)
    with pytest.raises(ValueError, match="ReserveCalifornia"):
        service.set_auto_pay(db, w.id, True)

    recgov.sites[BIG_SUR.id] = [site("50001", [FRI, FRI + timedelta(days=1)])]
    alerts = check(db, recgov)
    assert [a.watch_id for a in alerts] == [w.id]
    stay = alerts[0].stays[0].stay
    assert stay.url == "https://www.reservecalifornia.com/park/1001/2001"
    assert alerts[0].auto_hold is False
