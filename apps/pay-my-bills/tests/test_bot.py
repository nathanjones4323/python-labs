"""The bot against fake Telegram objects: who it answers, what it sends, and
what the buttons do."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import at, water_bill, water_paid
from pay_my_bills.bot import BillsBot
from pay_my_bills.config import BotSettings
from pay_my_bills.db import SplitRequest
from pay_my_bills.gmail import AuthenticationRequired
from telegram.ext import ApplicationHandlerStop
from test_pipeline import FakeMailbox

ME = 111


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(text_html="💧 <b>Ventura Water</b>")
        self.answered: list[str | None] = []
        self.edited: list[str] = []

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)
        self.markup = kwargs.get("reply_markup")


def make_bot(db, settings, mailbox, now="2026-10-11 08:00") -> BillsBot:
    return BillsBot(
        db,
        mailbox,
        settings,
        BotSettings(
            telegram_token="t", allowed_user_ids=frozenset({ME}), poll_seconds=120
        ),
        clock=lambda: at(now),
    )


def test_gate_drops_strangers(db, settings):
    bot = make_bot(db, settings, FakeMailbox)
    stranger = SimpleNamespace(effective_user=SimpleNamespace(id=999))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot._gate(stranger, None))
    me = SimpleNamespace(effective_user=SimpleNamespace(id=ME))
    asyncio.run(bot._gate(me, None))


def test_poll_sends_the_request_with_its_buttons(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    bot = make_bot(db, settings, lambda: mailbox)
    telegram = FakeBot()

    assert asyncio.run(bot.poll_once(telegram)) == 2
    [(chat_id, text, kwargs)] = telegram.sent
    assert chat_id == ME and "$28.60" in text
    rows = kwargs["reply_markup"].inline_keyboard
    assert "amount=28.60" in rows[0][0].url
    request_id = 0
    with db() as session:
        request_id = session.query(SplitRequest).one().id
    assert [b.callback_data for b in rows[1]] == [
        f"done:{request_id}",
        f"paid:{request_id}",
        f"skip:{request_id}",
    ]

    # Sent once.
    asyncio.run(bot.poll_once(telegram))
    assert len(telegram.sent) == 1


def test_a_gmail_problem_is_reported_once(db, settings):
    def broken():
        raise AuthenticationRequired("Run the login again.")

    bot = make_bot(db, settings, broken)
    telegram = FakeBot()
    assert asyncio.run(bot.poll_once(telegram)) is None
    asyncio.run(bot.poll_once(telegram))
    assert [text for _, text, _ in telegram.sent] == [
        "⚠️ Couldn't check Gmail: Run the login again."
    ]


def test_mark_requested_button(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    bot = make_bot(db, settings, lambda: mailbox)
    asyncio.run(bot.poll_once(FakeBot()))
    with db() as session:
        request_id = session.query(SplitRequest).one().id

    query = FakeQuery(f"done:{request_id}")
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert query.answered == ["📨 requested, waiting on payment"]
    assert query.edited[0].endswith("<i>📨 requested, waiting on payment</i>")
    with db() as session:
        assert session.get(SplitRequest, request_id).requested_at is not None
    # Still open: Paid and Skip stay on the message.
    [[paid, skip]] = query.markup.inline_keyboard
    assert (paid.callback_data, skip.callback_data) == (
        f"paid:{request_id}",
        f"skip:{request_id}",
    )

    query = FakeQuery(f"paid:{request_id}")
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert query.answered == ["✅ paid"]
    assert query.markup is None
    with db() as session:
        assert session.get(SplitRequest, request_id).settled_at is not None


def test_unknown_button_is_ignored(db, settings):
    bot = make_bot(db, settings, FakeMailbox)
    query = FakeQuery("nonsense")
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert query.answered == [None] and query.edited == []


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def test_status_has_paid_and_skip_for_each_open_request(db, settings):
    mailbox = FakeMailbox(water_bill(), water_paid())
    bot = make_bot(db, settings, lambda: mailbox)
    asyncio.run(bot.poll_once(FakeBot()))
    with db() as session:
        request_id = session.query(SplitRequest).one().id

    message = FakeMessage()
    asyncio.run(bot._status(SimpleNamespace(effective_message=message), None))
    [(text, kwargs)] = message.replies
    assert "Ventura Water Oct 2026" in text
    [[paid, skip]] = kwargs["reply_markup"].inline_keyboard
    assert paid.text == "✅ 💧 Oct $28.60 paid"
    assert paid.callback_data == f"paid:{request_id}:status"
    assert skip.callback_data == f"skip:{request_id}:status"

    # Pressing it redraws the list without it.
    query = FakeQuery(paid.callback_data)
    asyncio.run(bot._on_button(SimpleNamespace(callback_query=query), None))
    assert query.answered == ["✅ paid"]
    assert "Nothing open" in query.edited[0]
    assert query.markup is None


def test_status_with_nothing_open_has_no_buttons(db, settings):
    bot = make_bot(db, settings, FakeMailbox)
    message = FakeMessage()
    asyncio.run(bot._status(SimpleNamespace(effective_message=message), None))
    [(text, kwargs)] = message.replies
    assert "Nothing open" in text and kwargs["reply_markup"] is None
