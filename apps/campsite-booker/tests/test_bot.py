import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from html import escape
from types import SimpleNamespace

import pytest
from campsite_booker import booker, service
from campsite_booker.availability import StayCriteria
from campsite_booker.bot import CampsiteBot
from campsite_booker.checkout import CartItem, CheckoutResult
from campsite_booker.config import BotSettings
from campsite_booker.db import (
    BOOKED,
    CHALLENGED,
    DRY_RUN,
    FAILED,
    HELD,
    PAID,
    UNKNOWN,
    Watch,
)
from campsite_booker.gmail import AuthenticationRequired
from campsite_booker.recgov import CART_URL, Campground
from conftest import FakeMailbox, alert_message, site
from telegram.error import NetworkError
from telegram.ext import ApplicationHandlerStop

NOW = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)  # Oct 27, 6 PM Pacific
FRI = date(2026, 11, 6)
PINES = Campground(id="232447", name="Upper Pines")


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.fail_next = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.fail_next:
            self.fail_next -= 1
            raise NetworkError("down")
        self.sent.append((chat_id, text, reply_markup))


def buttons(markup) -> list[str]:
    return [b.callback_data or b.url for row in markup.inline_keyboard for b in row]


class Clock:
    """A clock that only moves when the bot sleeps."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def make_bot(db, recgov, settings):
    def make(hold_result=None, raises=None, pay_result=None, signed_in=True, **kw):
        held = []
        paid_with = []
        pays = []
        expected = []

        def hold(settings_, stay, pay_group_size):
            held.append(stay)
            paid_with.append(pay_group_size)
            if raises:
                raise raises
            return hold_result or booker.HoldResult(HELD, "In your cart", "u")

        def pay(settings_, order_url, group_size, label, expect=None):
            pays.append((order_url, group_size))
            expected.append(expect)
            return pay_result or CheckoutResult(PAID, "Paid", "1234-5678"), None

        clock = Clock(kw.pop("now", NOW))
        bot = CampsiteBot(
            db,
            recgov,
            replace(settings, **kw),
            BotSettings("token", frozenset({11}), 60),
            hold=hold,
            pay=pay,
            signed_in=lambda s: signed_in,
            clock=clock,
            sleep=clock.sleep,
        )
        bot.held, bot.paid_with, bot.pays, bot.test_clock = held, paid_with, pays, clock
        bot.expected = expected
        return bot, held

    return make


def add_watch(db, auto_hold=False) -> Watch:
    return service.create_watch(
        db, PINES, StayCriteria(FRI, FRI, 2), auto_hold=auto_hold
    )


def free(recgov, *ids):
    recgov.sites[PINES.id] = [
        site(i, [FRI, FRI + timedelta(days=1)], site=f"0{i}") for i in ids
    ]


def test_an_opening_is_sent_with_hold_buttons_and_marked_sent(db, recgov, make_bot):
    w = add_watch(db)
    free(recgov, "40", "41")
    bot, held = make_bot()
    tg = FakeTelegram()

    assert asyncio.run(bot.check_once(tg)) == 2
    assert len(tg.sent) == 1
    chat, text, markup = tg.sent[0]
    assert chat == 11
    assert "Open at Upper Pines" in text
    assert "Site 040" in text and "Fri Nov 6 → Sun Nov 8, 2 nights" in text
    # A row per stay: Open it on recreation.gov, or have the bot Hold it.
    rows = [[b.callback_data or b.url for b in row] for row in markup.inline_keyboard]
    assert rows[0][0].endswith("/camping/campsites/40") and rows[0][1].startswith(
        "hold:"
    )
    assert rows[1][0].endswith("/camping/campsites/41") and rows[1][1].startswith(
        "hold:"
    )
    assert rows[2] == [f"stop:{w.id}"]
    assert "Open one" in text
    assert held == []
    # Delivered: the next check sends nothing.
    assert asyncio.run(bot.check_once(tg)) == 0
    assert len(tg.sent) == 1


def test_auto_hold_holds_first_then_alerts(db, recgov, make_bot):
    w = add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, held = make_bot()
    tg = FakeTelegram()

    asyncio.run(bot.check_once(tg))
    assert [s.site for s in held] == ["040"]
    result, alert = tg.sent
    assert "Auto-held: In your cart" in result[1]
    # 6 PM Pacific + 15 minutes.
    assert "6:15 PM" in result[1]
    assert CART_URL in buttons(result[2])
    assert f"booked:{w.id}" in buttons(result[2])
    assert "Open at Upper Pines" in alert[1]
    holds = service.list_holds(db)
    assert [(h.status, h.automatic, h.watch_id) for h in holds] == [(HELD, True, w.id)]

    # The site it just held has no Hold button on the alert.
    assert "(in your cart)" in alert[1]
    assert "Open one" not in alert[1]
    assert not [d for d in buttons(alert[2]) if d.startswith("hold:")]

    # Another site opens: alerted, not held again.
    free(recgov, "40", "41")
    asyncio.run(bot.check_once(tg))
    assert len(held) == 1


def test_a_stay_already_in_the_cart_isnt_held_again(db, recgov, make_bot):
    add_watch(db)
    free(recgov, "40")
    bot, held = make_bot()
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    tap = [d for d in buttons(tg.sent[0][2]) if d.startswith("hold:")][0]

    press(bot, tg, tap)
    press(bot, tg, tap)
    assert len(held) == 1
    assert "Already done" in tg.sent[-1][1]
    assert len(service.list_holds(db)) == 1

    # Once the cart's 15 minutes are up it can be held again.
    bot.test_clock.now += timedelta(minutes=16)
    press(bot, tg, tap)
    assert len(held) == 2


def test_a_failed_hold_says_why(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(booker.HoldResult(FAILED, "Site no longer available", "u"))
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    assert "Couldn't hold" in tg.sent[0][1]
    assert "Site no longer available" in tg.sent[0][1]


def test_login_required_is_reported_and_nothing_recorded(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(raises=booker.LoginRequired("session expired"))
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    assert "Sign-in to recreation.gov needed" in tg.sent[0][1]
    assert "campsite-booker-login" in tg.sent[0][1]
    assert service.list_holds(db) == []


def test_an_alert_that_couldnt_be_sent_goes_on_the_next_check(
    db, recgov, make_bot, monkeypatch
):
    monkeypatch.setattr("campsite_booker.bot.SEND_RETRY_S", 0)
    add_watch(db)
    free(recgov, "40")
    bot, _ = make_bot()
    tg = FakeTelegram()
    tg.fail_next = 10
    asyncio.run(bot.check_once(tg))
    assert tg.sent == []
    tg.fail_next = 0
    asyncio.run(bot.check_once(tg))
    assert len(tg.sent) == 1 and "Site 040" in tg.sent[0][1]


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(text_html="🏕 alert")
        self.answers: list[str | None] = []
        self.edits: list[str] = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


def press(bot, tg, data: str) -> FakeQuery:
    query = FakeQuery(data)
    update = SimpleNamespace(callback_query=query)
    asyncio.run(bot._on_button(update, SimpleNamespace(bot=tg)))
    return query


def test_hold_button_holds_that_opening(db, recgov, make_bot):
    w = add_watch(db)
    free(recgov, "40", "41")
    bot, held = make_bot()
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    second = [d for d in buttons(tg.sent[0][2]) if d.startswith("hold:")][1]

    press(bot, tg, second)
    assert [s.site for s in held] == ["041"]
    assert "Putting site 041" in tg.sent[1][1]
    assert "In your cart" in tg.sent[2][1]
    assert service.list_holds(db)[0].automatic is False

    query = press(bot, tg, f"booked:{w.id}")
    assert query.answers == [f"Stopped watching #{w.id}"]
    assert "Booked" in query.edits[0]
    with db() as session:
        assert session.get(Watch, w.id).ended_reason == BOOKED


def test_strangers_are_ignored(make_bot):
    bot, _ = make_bot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot._gate(update, None))
    ok = SimpleNamespace(effective_user=SimpleNamespace(id=11))
    asyncio.run(bot._gate(ok, None))


def test_auto_pay_pays_for_the_automatic_hold(db, recgov, make_bot):
    w = service.create_watch(
        db, PINES, StayCriteria(FRI, FRI, 2, group_size=4), auto_hold=True
    )
    service.set_auto_pay(db, w.id, True)
    free(recgov, "40")
    paid = booker.HoldResult(
        HELD, "In your cart", "u", checkout=CheckoutResult(PAID, "Paid", "0042-77")
    )
    bot, _ = make_bot(hold_result=paid, submit_payment=True)
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    assert bot.paid_with == [4]
    assert "Booked and paid" in tg.sent[0][1] and "0042-77" in tg.sent[0][1]
    assert service.list_holds(db)[0].checkout_status == PAID


def test_without_auto_pay_the_hold_offers_a_pay_button(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(pay_result=CheckoutResult(DRY_RUN, "Dry run: stopped"))
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    assert bot.paid_with == [None]
    pay = [b for b in buttons(tg.sent[0][2]) if b.startswith("pay:")]
    assert pay and "Pay (dry run)" in str(tg.sent[0][2])

    query = press(bot, tg, pay[0])
    assert query.answers == ["Dry run…"]
    assert query.markup_cleared  # each Pay button pays once
    assert bot.pays == [("u", 2)]  # the hold's page; the default group size
    assert "Dry run: stopped" in tg.sent[-1][1]
    assert service.list_holds(db)[0].checkout_status == DRY_RUN


def test_a_paid_hold_isnt_paid_twice(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(submit_payment=True)
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    hold_id = service.list_holds(db)[0].id
    press(bot, tg, f"pay:{hold_id}")
    press(bot, tg, f"pay:{hold_id}")
    assert len(bot.pays) == 1
    assert "already paid" in tg.sent[-1][1]


def test_an_unknown_checkout_isnt_offered_or_paid_again(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(
        submit_payment=True, pay_result=CheckoutResult(UNKNOWN, "No confirmation")
    )
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    hold_id = service.list_holds(db)[0].id
    press(bot, tg, f"pay:{hold_id}")
    assert not [b for b in buttons(tg.sent[-1][2]) if b.startswith("pay:")]
    press(bot, tg, f"pay:{hold_id}")
    assert len(bot.pays) == 1
    assert "may have paid" in tg.sent[-1][1]


def test_a_failed_hold_isnt_paid(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(booker.HoldResult(FAILED, "Gone", "u"))
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    press(bot, tg, f"pay:{service.list_holds(db)[0].id}")
    assert bot.pays == []
    assert "nothing to pay" in tg.sent[-1][1]


def test_a_challenged_hold_hands_over_then_pays_from_the_cart(db, recgov, make_bot):
    w = add_watch(db, auto_hold=True)
    free(recgov, "40")
    bot, _ = make_bot(
        booker.HoldResult(CHALLENGED, "recreation.gov asked for a human check", "u"),
        pay_result=CheckoutResult(DRY_RUN, "Dry run: stopped"),
    )
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    result, alert = tg.sent
    assert result[1].startswith("🧩 Tried to auto-hold: <b>recreation.gov wants")
    assert "isn't held yet" in result[1]
    data = buttons(result[2])
    assert data[0].endswith("/camping/campsites/40")
    assert "It's in my cart: pay (dry run)" in str(result[2])
    assert f"booked:{w.id}" in data
    # Not in the cart, so the alert still offers it.
    assert "(in your cart)" not in alert[1]
    assert [d for d in buttons(alert[2]) if d.startswith("hold:")]

    # You add it on recreation.gov and tap "It's in my cart".
    hold = service.list_holds(db)[0]
    press(bot, tg, f"pay:{hold.id}")
    assert bot.pays == [("u", 2)]
    # Paying checks the cart has that site and check-in.
    assert bot.expected == [CartItem("040", FRI)]
    hold = service.list_holds(db)[0]
    assert (hold.status, hold.checkout_status) == (HELD, DRY_RUN)
    assert "In your cart" in tg.sent[-1][1]


def test_a_challenged_hold_thats_not_in_the_cart_stays_challenged(db, recgov, make_bot):
    add_watch(db, auto_hold=True)
    free(recgov, "40")
    missing = "Site 040 arriving Fri Nov 06, 2026 isn't in your cart"
    bot, _ = make_bot(
        booker.HoldResult(CHALLENGED, "human check", "u"),
        pay_result=CheckoutResult(FAILED, missing),
    )
    tg = FakeTelegram()
    asyncio.run(bot.check_once(tg))
    hold = service.list_holds(db)[0]
    press(bot, tg, f"pay:{hold.id}")
    assert service.list_holds(db)[0].status == CHALLENGED
    assert escape(missing) in tg.sent[-1][1]
    # Still offered, for once it's added.
    assert f"pay:{hold.id}" in buttons(tg.sent[-1][2])


RELEASE_AT = datetime(2026, 11, 15, 15, 0, tzinfo=UTC)  # 7 AM Pacific
MARCH = date(2027, 3, 19)


def releases_for(recgov):
    recgov.release_payloads[PINES.id] = {
        "current_release": {"end": "2027-03-14T00:00:00Z"},
        "next_release": {
            "release_time": "2026-11-15T10:00:00-05:00",
            "end": "2027-04-14T00:00:00Z",
        },
    }


def march(recgov, status: str):
    s = site("40", [], site="040")
    s.nights = {
        date(2027, 3, 15): status,
        MARCH: status,
        MARCH + timedelta(days=1): status,
    }
    recgov.sites[PINES.id] = [s]


def test_a_watch_on_unreleased_dates_is_announced_once(db, recgov, make_bot):
    service.create_watch(db, PINES, StayCriteria(MARCH, MARCH, 2))
    releases_for(recgov)
    bot, _ = make_bot()
    tg = FakeTelegram()

    async def go():
        await bot.arm(tg)
        await bot.arm(tg)
        await bot.stop_polling()

    asyncio.run(go())
    assert len(tg.sent) == 1
    assert "go on sale" in tg.sent[0][1] and "Sun Nov 15 7:00 AM" in tg.sent[0][1]


def test_a_release_burst_holds_the_moment_the_dates_appear(db, recgov, make_bot):
    w = service.create_watch(db, PINES, StayCriteria(MARCH, MARCH, 2), auto_hold=True)
    releases_for(recgov)
    march(recgov, "NYR")
    bot, held = make_bot(release_poll_seconds=2)
    tg = FakeTelegram()
    asyncio.run(bot.arm(tg))  # announces, and schedules the burst
    next(iter(bot._bursts.values())).cancel()
    clock = bot.test_clock
    clock.now = NOW  # the scheduled burst ran a step before it was cancelled
    polls = []

    async def sleep(seconds):
        await clock.sleep(seconds)
        polls.append(clock.now)
        # Sites appear a few seconds after the release.
        if clock.now >= RELEASE_AT + timedelta(seconds=4):
            march(recgov, "Available")

    bot.sleep = sleep
    release = bot._releases[PINES.id][1]
    asyncio.run(bot._burst(tg, PINES.id, release))

    # Slept to 10 minutes before (sign-in check), then to 20 s before.
    assert polls[0] == RELEASE_AT - timedelta(minutes=10)
    assert polls[1] == RELEASE_AT - timedelta(seconds=20)
    # Then every 2 s, until the dates were on sale and the site held.
    assert [s.site for s in held] == ["040"]
    assert polls[-1] - polls[-2] == timedelta(seconds=2)
    holds = service.list_holds(db)
    assert [(h.automatic, h.watch_id) for h in holds] == [(True, w.id)]
    assert any("Open at Upper Pines" in t for _, t, _ in tg.sent)


def test_a_burst_warns_when_signed_out_ahead_of_the_release(db, recgov, make_bot):
    service.create_watch(db, PINES, StayCriteria(MARCH, MARCH, 2))
    releases_for(recgov)
    march(recgov, "Reserved")  # already out: the burst ends on its first check
    bot, _ = make_bot(signed_in=False)
    tg = FakeTelegram()
    asyncio.run(bot.arm(tg))
    next(iter(bot._bursts.values())).cancel()
    bot.test_clock.now = NOW
    asyncio.run(bot._burst(tg, PINES.id, bot._releases[PINES.id][1]))
    assert any("Sign-in to recreation.gov needed" in t for _, t, _ in tg.sent)


EMAIL_AT = datetime(2026, 10, 27, 23, 3, 53, tzinfo=UTC)


def test_an_alert_email_triggers_a_check_and_is_timed(db, recgov, make_bot):
    upper = Campground(id="232447", name="Upper Pines Campground")
    service.create_watch(
        db, upper, StayCriteria(date(2026, 10, 28), date(2026, 10, 28), 1)
    )
    recgov.sites[upper.id] = [site("94", [date(2026, 10, 28)], site="037")]
    mailbox = FakeMailbox()
    mailbox.messages["m1"] = alert_message("m1", EMAIL_AT)
    bot, _ = make_bot(now=EMAIL_AT + timedelta(seconds=8))
    bot.mailbox = lambda: mailbox
    tg = FakeTelegram()

    assert asyncio.run(bot.check_email(tg)) == 1
    assert "Site 037" in tg.sent[0][1]
    assert "recreation.gov's alert email" in tg.sent[0][1]
    [row] = service.timing_summary(db, EMAIL_AT - timedelta(days=1)).rows
    assert (row.outcome, row.lead_seconds, row.triggered_finds) == (
        "email_first",
        -8,
        1,
    )
    # Nothing new the next time.
    assert asyncio.run(bot.check_email(tg)) == 0
    assert len(tg.sent) == 1


def test_a_gmail_sign_in_problem_is_reported_once(db, recgov, make_bot):
    bot, _ = make_bot()

    def broken():
        raise AuthenticationRequired("No Gmail sign-in saved")

    bot.mailbox = broken
    tg = FakeTelegram()
    asyncio.run(bot.check_email(tg))
    asyncio.run(bot.check_email(tg))
    assert len(tg.sent) == 1 and "alert emails" in tg.sent[0][1]
