"""The Telegram bot, without Telegram or Claude: the tool allowlist, the
payment gate, the conversation's bookkeeping and the run messages."""

import asyncio
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("telegram")
fastmcp = pytest.importorskip("fastmcp")

import anthropic  # noqa: E402
import httpx  # noqa: E402
from tee_time_booker.api import create_app  # noqa: E402
from tee_time_booker.bot import agent as agent_module  # noqa: E402
from tee_time_booker.bot import app as app_module  # noqa: E402
from tee_time_booker.bot.agent import (  # noqa: E402
    BookingAgent,
    StartedRun,
    TeeTimeOffer,
)
from tee_time_booker.bot.app import (  # noqa: E402
    BookingBot,
    format_offer,
    format_outcome,
    format_runs,
    send_with_retry,
    slot_label,
)
from tee_time_booker.bot.rich import Progress, escape, escape_prices  # noqa: E402
from tee_time_booker.bot.tools import ALLOWED_TOOLS, BookingTools, ToolResult  # noqa: E402
from tee_time_booker.config import BotSettings, Settings  # noqa: E402
from tee_time_booker.mcp import create_server  # noqa: E402
from tee_time_booker.scheduler import Started  # noqa: E402
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

RUN_STARTED = {
    "run_id": "11111111-1111-1111-1111-111111111111",
    "request_id": "22222222-2222-2222-2222-222222222222",
    "course": "soule-park",
    "target_date": "2026-10-24",
    "dry_run": False,
    "wait_for_release": True,
}

BOT_SETTINGS = BotSettings(
    telegram_token="token",
    allowed_user_ids=frozenset({42}),
    user_email="golfer@example.com",
    model="claude-sonnet-5-5",
    effort="medium",
    whisper_model="base.en",
)


def _block(**kwargs):
    return SimpleNamespace(**kwargs)


def _tool_use(name, arguments, id_="call_1"):
    return _block(type="tool_use", id=id_, name=name, input=arguments)


def _text(text):
    return _block(type="text", text=text)


class FakeClaude:
    """Answers each `messages.create` with the next scripted response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **request):
        # Copy: the agent keeps appending to the list it passed.
        self.requests.append({**request, "messages": list(request["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        content, stop_reason = response
        return SimpleNamespace(content=content, stop_reason=stop_reason)


class FakeTools:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    async def definitions(self):
        return [{"name": "book_plan", "description": "", "input_schema": {}}]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.results.get(name, ToolResult(json.dumps(RUN_STARTED)))


def _agent(claude, tools, *, pays=True):
    return BookingAgent(
        claude, tools, BOT_SETTINGS, pays=pays, today=lambda: date(2026, 10, 20)
    )


def _respond(agent, text, chat_id=7):
    return asyncio.run(agent.respond(chat_id, text))


def test_a_paying_call_is_held_not_run():
    claude = FakeClaude(
        (
            [_tool_use("book_plan", {"plan_id": "p", "submit_payment": True})],
            "tool_use",
        ),
        ([_text("Tap Confirm to book.")], "end_turn"),
    )
    tools = FakeTools()
    reply = _respond(_agent(claude, tools), "book it for real")

    assert tools.calls == []
    assert reply.text == "Tap Confirm to book."
    [payment] = reply.payments
    assert (payment.tool, payment.arguments["plan_id"]) == ("book_plan", "p")
    # The model is told it's held, not that it ran.
    tool_result = claude.requests[1]["messages"][-1]["content"][0]
    assert "confirmation" in tool_result["content"]
    assert reply.runs == []


def test_a_dry_run_runs_without_asking_and_is_watched():
    claude = FakeClaude(
        (
            [_tool_use("book_plan", {"plan_id": "p", "submit_payment": False})],
            "tool_use",
        ),
        ([_text("Started a dry run.")], "end_turn"),
    )
    tools = FakeTools()
    reply = _respond(_agent(claude, tools), "try booking")

    assert tools.calls == [("book_plan", {"plan_id": "p", "submit_payment": False})]
    assert reply.payments == []
    assert reply.runs == [
        StartedRun(
            "11111111-1111-1111-1111-111111111111", "soule-park", "2026-10-24", False
        )
    ]


def test_with_payment_switched_off_nothing_needs_confirming():
    # Every run is a dry run then, so there's nothing to confirm.
    claude = FakeClaude(
        (
            [_tool_use("book_plan", {"plan_id": "p", "submit_payment": True})],
            "tool_use",
        ),
        ([_text("ok")], "end_turn"),
    )
    tools = FakeTools()
    reply = _respond(_agent(claude, tools, pays=False), "book it")
    assert len(tools.calls) == 1
    assert reply.payments == []
    assert "switched off" in claude.requests[0]["system"]


def test_confirming_runs_the_held_call_once_and_tells_the_conversation():
    claude = FakeClaude(
        (
            [_tool_use("book_plan", {"plan_id": "p", "submit_payment": True})],
            "tool_use",
        ),
        ([_text("Tap Confirm.")], "end_turn"),
        ([_text("Booked.")], "end_turn"),
    )
    tools = FakeTools()
    agent = _agent(claude, tools)
    [payment] = _respond(agent, "book it").payments

    taken = agent.take_payment(payment.id)
    assert agent.take_payment(payment.id) is None  # a button works once
    result, run = asyncio.run(agent.confirm(taken))

    assert tools.calls == [("book_plan", {"plan_id": "p", "submit_payment": True})]
    assert run.run_id == RUN_STARTED["run_id"]
    _respond(agent, "thanks")
    sent = claude.requests[-1]["messages"]
    assert any(
        "tapped Confirm" in str(m["content"]) for m in sent if m["role"] == "user"
    )


def test_a_stale_button_does_nothing(monkeypatch):
    claude = FakeClaude(
        (
            [_tool_use("run_request", {"request_id": "r", "submit_payment": True})],
            "tool_use",
        ),
        ([_text("Tap Confirm.")], "end_turn"),
    )
    agent = _agent(claude, FakeTools())
    [payment] = _respond(agent, "book it").payments
    monkeypatch.setattr(agent_module, "PAYMENT_TTL_S", -1)
    assert agent.take_payment(payment.id) is None


def test_a_failed_request_leaves_the_history_as_it_was():
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    claude = FakeClaude(
        ([_text("hi")], "end_turn"),
        ([_tool_use("list_plans", {})], "tool_use"),
        error,
        ([_text("still here")], "end_turn"),
    )
    agent = _agent(claude, FakeTools({"list_plans": ToolResult("[]")}))
    _respond(agent, "hello")
    reply = _respond(agent, "my plans?")
    assert "Couldn't reach Claude" in reply.text

    _respond(agent, "again")
    # The failed turn (with its unanswered tool call) was never kept.
    roles = [m["role"] for m in claude.requests[-1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_reset_forgets_the_conversation_and_its_buttons():
    claude = FakeClaude(
        (
            [_tool_use("book_plan", {"plan_id": "p", "submit_payment": True})],
            "tool_use",
        ),
        ([_text("Tap Confirm.")], "end_turn"),
        ([_text("hi")], "end_turn"),
    )
    agent = _agent(claude, FakeTools())
    [payment] = _respond(agent, "book it").payments
    agent.reset(7)
    assert agent.take_payment(payment.id) is None
    _respond(agent, "hello")
    assert len(claude.requests[-1]["messages"]) == 1


def test_strangers_are_dropped_before_any_handler():
    bot = BookingBot(None, None, None, BOT_SETTINGS, pays=False)
    stranger = SimpleNamespace(effective_user=SimpleNamespace(id=999, username="x"))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot._gate(stranger, None))
    owner = SimpleNamespace(effective_user=SimpleNamespace(id=42, username="me"))
    asyncio.run(bot._gate(owner, None))  # no raise


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "success",
            r"✅ **Booked** soule\-park on Sat Oct 24 at **9:12 AM \(Front\)**.",
        ),
        ("dry_run", r"**Dry run done:** reached checkout for 9:12 AM \(Front\)"),
        ("unknown", "Check your account on the booking site"),
        (
            "error",
            r"❌ **The run failed** for soule\-park on Sat Oct 24."
            "\n\n<blockquote expandable>login &lt;rejected&gt;</blockquote>",
        ),
    ],
)
def test_run_outcomes_read_plainly(status, expected):
    started = StartedRun("r", "soule-park", "2026-10-24", dry_run=status == "dry_run")
    run = {
        "status": status,
        "tee_time_local": "2026-10-24T09:12:00",
        "side": "Front",
        "green_fee": None,
        "confirmation": None,
        "detail": "login <rejected>",
    }
    assert expected in format_outcome(started, run)


def test_a_booking_shows_its_fee_and_a_copyable_confirmation():
    started = StartedRun("r", "soule-park", "2026-10-24", dry_run=False)
    run = {
        "status": "success",
        "tee_time_local": None,
        "green_fee": "20.75",
        "confirmation": "AB`12",
    }
    text = format_outcome(started, run)
    assert r"Green fee \$20.75 per player." in text
    assert text.endswith("Confirmation: `AB12`")


def test_escape_keeps_markdown_out_of_values():
    assert escape("a*b_c|d<e>") == r"a\*b\_c\|d\<e\>"


def test_recent_runs_are_a_table():
    runs = [
        {
            "status": "success",
            "started_at": "2026-10-20T16:00:00+00:00",
            "tee_time_local": "2026-10-24T09:12:00",
            "side": None,
        },
        {"status": "error", "started_at": "2026-10-20T16:00:00+00:00"},
    ]
    lines = format_runs(runs).splitlines()
    assert lines[0] == "| Started | Status | Slot |"
    assert "| ✅ booked | 9:12 AM |" in lines[2]
    assert lines[3].endswith("| ❌ failed | – |")
    assert format_runs([]) == "No runs yet."


class FlakyTelegram:
    """Fails each send with the next scripted error, then sends.

    Rich messages go through `do_api_request`; plain ones through
    `send_message`.
    """

    def __init__(self, *errors):
        self.errors = list(errors)
        self.sent = []
        self.plain = []
        self.requests = []

    def _fail(self):
        if self.errors:
            raise self.errors.pop(0)

    async def do_api_request(self, endpoint, api_kwargs=None):
        self._fail()
        self.requests.append((endpoint, api_kwargs))
        if endpoint == "sendRichMessage":
            self.sent.append(api_kwargs["rich_message"]["markdown"])

    async def send_message(self, chat_id, text, **kwargs):
        self._fail()
        self.plain.append((text, kwargs))


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)
    return waits


def test_a_send_rides_out_a_dropped_connection_and_a_rate_limit(no_sleep):
    telegram = FlakyTelegram(
        NetworkError("httpx.ConnectError"), TimedOut(), RetryAfter(5)
    )
    asyncio.run(send_with_retry(telegram, 7, "Dry run done"))
    assert telegram.sent == ["Dry run done"]
    assert no_sleep == [1.0, 2.0, 5]


def test_a_send_is_a_rich_message():
    telegram = FlakyTelegram()
    asyncio.run(send_with_retry(telegram, 7, "**Booked**", reply_markup="kb"))
    [(endpoint, kwargs)] = telegram.requests
    assert endpoint == "sendRichMessage"
    assert kwargs == {
        "chat_id": 7,
        "rich_message": {"markdown": "**Booked**"},
        "reply_markup": "kb",
    }


def test_the_models_prices_stay_out_of_formulas():
    # "$20 or $25" would otherwise be a formula; an escaped one is left alone.
    assert escape_prices(r"$20 or \$25, $x") == r"\$20 or \$25, $x"


def test_template_html_carries_no_backslashes():
    assert "\\" not in format_offer(SOULE_OFFER, SOULE_NAMES).split("<table")[1]


def test_refused_formatting_falls_back_to_plain_text(no_sleep, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_MESSAGE", 3)
    telegram = FlakyTelegram(BadRequest("can't parse entities"))
    asyncio.run(send_with_retry(telegram, 7, "abcdef", reply_markup="kb"))
    # Split to the plain limit, buttons on the last part only.
    assert telegram.plain == [("abc", {}), ("def", {"reply_markup": "kb"})]
    assert no_sleep == []


def test_a_bad_message_is_not_retried(no_sleep):
    telegram = FlakyTelegram(
        BadRequest("message is too long"), BadRequest("message is too long")
    )
    with pytest.raises(BadRequest):
        asyncio.run(send_with_retry(telegram, 7, "x"))
    assert no_sleep == []


def test_progress_names_the_step_and_shrugs_off_a_failed_draft():
    telegram = FlakyTelegram()

    async def go():
        async with Progress(telegram, 7, 99) as progress:
            telegram.errors.append(BadRequest("drafts unsupported"))
            await progress.step([_tool_use("find_tee_times", {})])  # no raise
            await progress.step(
                [_tool_use("list_plans", {}), _tool_use("get_plan", {}, "call_2")]
            )

    asyncio.run(go())
    drafts = [kwargs for endpoint, kwargs in telegram.requests]
    assert [d["draft_id"] for d in drafts] == [99, 99]
    assert [d["rich_message"]["markdown"] for d in drafts] == [
        "<tg-thinking>Thinking…</tg-thinking>",
        "<tg-thinking>Looking at your plans…</tg-thinking>",
    ]


def test_each_round_of_tool_calls_is_announced_before_it_runs():
    claude = FakeClaude(
        ([_tool_use("list_plans", {})], "tool_use"),
        ([_text("You have one plan.")], "end_turn"),
    )
    tools = FakeTools({"list_plans": ToolResult("[]")})
    steps = []

    async def on_step(calls):
        steps.append(([c.name for c in calls], len(tools.calls)))

    asyncio.run(_agent(claude, tools).respond(7, "my plans?", on_step=on_step))
    assert steps == [(["list_plans"], 0)]


def test_a_send_gives_up_after_the_retry_window(no_sleep, monkeypatch):
    monkeypatch.setattr(app_module, "SEND_RETRY_S", 0)
    telegram = FlakyTelegram(NetworkError("down"), NetworkError("still down"))
    with pytest.raises(NetworkError, match="down"):
        asyncio.run(send_with_retry(telegram, 7, "x"))
    assert telegram.sent == []


def test_bot_settings_need_numeric_user_ids(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("TEE_TIME_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456")
    assert BotSettings.from_env().allowed_user_ids == {123, 456}
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "@me")
    with pytest.raises(RuntimeError, match="numeric"):
        BotSettings.from_env()


def test_the_real_tools_are_the_allowlist_and_answer(seeded, db, tmp_path: Path):
    """Over the actual MCP server and a seeded database."""
    settings = Settings(
        database_url=None,
        plan_id=None,
        submit_payment=False,
        headless=True,
        artifacts_dir=tmp_path,
        target_date_override=None,
    )

    async def go():
        async with fastmcp.Client(
            create_server(create_app(settings, sessions=db))
        ) as client:
            tools = BookingTools(client)
            names = [t["name"] for t in await tools.definitions()]
            plans = (await tools.call("list_plans", {})).json()
            refused = await tools.call("create_plan", {})
            return names, plans, refused

    names, plans, refused = asyncio.run(go())
    assert names == sorted(ALLOWED_TOOLS)
    assert {"create_plan", "search_tee_times"} <= set(names)
    assert {p["name"] for p in plans} == {"soule-saturday", "saticoy-saturday"}
    # Missing its required arguments.
    assert refused.is_error


FOUND = [
    {
        "start": "2026-11-01T07:40:00",
        "holes": 18,
        "players": 4,
        "green_fee": 65.0,
        "service_fee": 2.5,
        "side": "Front",
    },
    {
        "start": "2026-11-01T08:20:00",
        "holes": 18,
        "players": 4,
        "green_fee": 58.0,
        "service_fee": None,
        "side": None,
    },
]
PLAN = {
    "id": "p",
    "user_email": "golfer@example.com",
    "course": "soule-park",
    "env_prefix": "SOULE",
    "holes": 18,
    "players": 2,
}
SOULE_OFFER = TeeTimeOffer(
    "o",
    7,
    {"soule-park": PLAN},
    tuple({**slot, "course": "soule-park"} for slot in FOUND),
)
SOULE_NAMES = {"soule-park": "Soule Park"}


def _offer_tools():
    return FakeTools(
        {
            "find_tee_times": ToolResult(json.dumps({"result": FOUND})),
            "get_plan": ToolResult(json.dumps(PLAN)),
            "create_request": ToolResult(json.dumps({"id": "req"})),
        }
    )


def _shown(times, tools, *, pays=True):
    """A search for Sunday, then a card of `times` from it."""
    claude = FakeClaude(
        (
            [_tool_use("find_tee_times", {"plan_id": "p", "date": "2026-11-01"})],
            "tool_use",
        ),
        (
            [
                _tool_use(
                    "show_tee_times",
                    {
                        "date": "2026-11-01",
                        "slots": [{"course": "soule-park", "time": t} for t in times],
                    },
                    "call_2",
                )
            ],
            "tool_use",
        ),
        ([], "end_turn"),
    )
    agent = _agent(claude, tools, pays=pays)
    return agent, claude, _respond(agent, "what's open Sunday?")


def test_shown_tee_times_come_only_from_the_search():
    agent, claude, reply = _shown(["8:20", "07:40", "10:00"], _offer_tools())
    [offer] = reply.offers
    assert [s["start"] for s in offer.slots] == [
        "2026-11-01T08:20:00",
        "2026-11-01T07:40:00",
    ]
    # A card with nothing more to say is fine.
    assert reply.text == ""
    tool_result = claude.requests[2]["messages"][-1]["content"][0]
    assert "10:00" in tool_result["content"]
    assert agent.offer(offer.id) is offer
    assert "show_tee_times" in [t["name"] for t in claude.requests[0]["tools"]]


def test_showing_tee_times_needs_a_search_first():
    claude = FakeClaude(
        (
            [
                _tool_use(
                    "show_tee_times",
                    {
                        "date": "2026-11-01",
                        "slots": [{"course": "soule-park", "time": "07:40"}],
                    },
                )
            ],
            "tool_use",
        ),
        ([_text("Let me look first.")], "end_turn"),
    )
    reply = _respond(_agent(claude, FakeTools()), "book 7:40")
    assert reply.offers == []
    assert claude.requests[1]["messages"][-1]["content"][0]["is_error"]


@pytest.mark.parametrize("pays", [True, False])
def test_booking_from_a_card_books_exactly_that_slot(pays):
    tools = _offer_tools()
    agent, _, reply = _shown(["07:40"], tools, pays=pays)
    [offer] = reply.offers
    payment = agent.hold_slot(offer, offer.slots[0])
    tools.calls.clear()

    _, run = asyncio.run(agent.confirm(agent.take_payment(payment.id)))

    assert run.run_id == RUN_STARTED["run_id"]
    # The plan's search said where and for how many; nothing is looked up again.
    assert tools.calls == [
        (
            "create_request",
            {
                "user_email": "golfer@example.com",
                "course": "soule-park",
                "env_prefix": "SOULE",
                "target_date": "2026-11-01",
                "earliest": "07:40:00",
                "latest": "07:40:00",
                "holes": 18,
                "players": 2,
            },
        ),
        (
            "run_request",
            {"request_id": "req", "submit_payment": pays, "wait_for_release": False},
        ),
    ]


def test_the_tee_time_card_is_a_table():
    text = format_offer(SOULE_OFFER, SOULE_NAMES)
    assert text.startswith("📅 **Soule Park · Sun Nov 1**\n\n2 times · 2 golfers")
    assert "<td><b>7:40 AM</b></td><td>Front</td>" in text
    assert '<td align="right"><b>$58</b></td>' in text
    assert "Course" not in text  # one course: it's in the title
    assert "tg-button" not in text  # Telegram doesn't send their taps
    assert slot_label(FOUND[0]) == "7:40 AM"


def test_a_side_column_only_when_the_sides_differ():
    same = tuple({**slot, "side": "Front"} for slot in SOULE_OFFER.slots)
    text = format_offer(replace(SOULE_OFFER, slots=same), SOULE_NAMES)
    assert "Side" not in text and "Front" not in text


def test_a_card_across_courses_numbers_its_rows():
    sterling = {**FOUND[1], "course": "sterling-hills"}
    offer = TeeTimeOffer(
        "o",
        7,
        {"soule-park": PLAN, "sterling-hills": {**PLAN, "course": "sterling-hills"}},
        (SOULE_OFFER.slots[0], sterling),
    )
    names = {**SOULE_NAMES, "sterling-hills": "Sterling Hills Golf Club"}
    text = format_offer(offer, names)
    assert text.startswith(
        "📅 **Sun Nov 1**\n\n2 times · 2 golfers · 18 holes · 2 courses"
    )
    assert "<td>2</td><td>Sterling Hills Golf Club</td><td><b>8:20 AM</b></td>" in text
    assert slot_label(sterling, 2) == "#2 8:20 AM"


def test_outcomes_name_the_course():
    started = StartedRun("r", "soule-park", "2026-10-24", dry_run=True)
    run = {"status": "dry_run", "tee_time_local": None}
    assert "at Soule Park on Sat Oct 24" in format_outcome(started, run, "Soule Park")


# --- searching by course ---------------------------------------------------


SEARCHED = {
    "tee_times": [
        {**FOUND[0], "course": "olivas-links", "course_name": "Olivas Links"},
        {**FOUND[1], "course": "sterling-hills", "course_name": "Sterling Hills"},
        {**FOUND[1], "course": "olivas-links", "course_name": "Olivas Links"},
    ],
    "searched": ["olivas-links", "sterling-hills"],
    "not_searched": {"soule-park": "golfer@example.com has no foreup account"},
}


def test_one_search_across_courses_makes_one_card_that_books_any_of_them():
    tools = FakeTools(
        {
            "search_tee_times": ToolResult(json.dumps(SEARCHED)),
            "create_request": ToolResult(json.dumps({"id": "req"})),
        }
    )
    search = {
        "user_email": "golfer@example.com",
        "date": "2026-11-01",
        "players": 3,
        "holes": 18,
        "earliest": "07:00",
        "latest": "09:00",
    }
    show = {
        "date": "2026-11-01",
        "slots": [
            {"course": "sterling-hills", "time": "08:20"},
            {"course": "olivas-links", "time": "07:40"},
            {"course": "soule-park", "time": "08:00"},  # never searched
        ],
    }
    claude = FakeClaude(
        ([_tool_use("search_tee_times", search)], "tool_use"),
        ([_tool_use("show_tee_times", show, "call_2")], "tool_use"),
        ([], "end_turn"),
    )
    agent = _agent(claude, tools)
    [offer] = _respond(agent, "anything around 8 on Sunday?").offers
    assert [(s["course"], s["start"][11:16]) for s in offer.slots] == [
        ("sterling-hills", "08:20"),
        ("olivas-links", "07:40"),
    ]
    assert set(offer.searches) == {"sterling-hills", "olivas-links"}
    left_out = claude.requests[2]["messages"][-1]["content"][0]["content"]
    assert "soule-park 08:00" in left_out
    tools.calls.clear()

    payment = agent.hold_slot(offer, offer.slots[1])
    asyncio.run(agent.confirm(agent.take_payment(payment.id)))
    name, request = tools.calls[0]
    assert name == "create_request"
    # No env_prefix: the API picks the person's only account on that site.
    assert "env_prefix" not in request
    assert (request["course"], request["players"], request["earliest"]) == (
        "olivas-links",
        3,
        "07:40:00",
    )


# --- scheduling ------------------------------------------------------------

SCHEDULE = {
    "user_email": "golfer@example.com",
    "course": "glen-annie",
    "target_date": "2026-11-14",
    "earliest": "08:00",
    "latest": "10:00",
    "holes": 18,
    "players": 2,
}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_request", {**SCHEDULE, "auto_book": True}),
        ("create_plan", {"name": "x", "auto_book": True}),
        ("set_plan_auto_book", {"plan_id": "p", "enabled": True}),
    ],
)
def test_scheduling_to_pay_later_is_held_like_paying_now(tool, arguments):
    claude = FakeClaude(
        ([_tool_use(tool, arguments)], "tool_use"),
        ([_text("Tap Confirm.")], "end_turn"),
    )
    tools = FakeTools()
    reply = _respond(_agent(claude, tools), "book it when it opens")
    assert tools.calls == []
    assert [p.tool for p in reply.payments] == [tool]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_request", SCHEDULE),
        ("set_plan_auto_book", {"plan_id": "p", "enabled": False}),
    ],
)
def test_not_scheduling_or_pausing_runs_straight_away(tool, arguments):
    claude = FakeClaude(
        ([_tool_use(tool, arguments)], "tool_use"),
        ([_text("Done.")], "end_turn"),
    )
    tools = FakeTools({tool: ToolResult(json.dumps({"id": "x"}))})
    reply = _respond(_agent(claude, tools), "pause it")
    assert [name for name, _ in tools.calls] == [tool]
    assert reply.payments == [] and reply.runs == []


def test_confirming_a_schedule_starts_no_run():
    scheduled = {**SCHEDULE, "id": "req", "first_attempt_at": "2026-10-15T06:00:00"}
    claude = FakeClaude(
        ([_tool_use("create_request", {**SCHEDULE, "auto_book": True})], "tool_use"),
        ([_text("Tap Confirm.")], "end_turn"),
        ([_text("ok")], "end_turn"),
    )
    tools = FakeTools({"create_request": ToolResult(json.dumps(scheduled))})
    agent = _agent(claude, tools)
    [payment] = _respond(agent, "book it when it opens").payments

    result, run = asyncio.run(agent.confirm(agent.take_payment(payment.id)))

    assert run is None and not result.is_error
    assert tools.calls == [("create_request", {**SCHEDULE, "auto_book": True})]
    _respond(agent, "thanks")
    sent = claude.requests[-1]["messages"]
    assert any(
        "it's scheduled" in str(m["content"]) for m in sent if m["role"] == "user"
    )


def test_the_schedule_card_says_what_it_will_book():
    one_off = app_module.schedule_lines("create_request", SCHEDULE, "Glen Annie")
    assert one_off.startswith("**⏰ Book this automatically?**")
    assert "<td>Sat Nov 14</td>" in one_off
    assert "8:00 AM – 10:00 AM" in one_off and "without asking again" in one_off

    plan = {
        **SCHEDULE,
        "target_weekday": 6,
        "weeks_ahead": 3,
        "next_target_date": "2026-11-08",
    }
    weekly = app_module.schedule_lines("create_plan", plan, "Glen Annie")
    assert "every week" in weekly
    assert "<td>Sundays, the 3rd ahead</td>" in weekly
    assert "<td>Sun Nov 8</td>" in weekly


def test_a_confirmed_schedule_says_when_it_first_tries():
    later = {
        "target_date": "2099-11-14",
        "first_attempt_at": "2099-10-24T23:50:00-07:00",
    }
    assert app_module.scheduled_status(later) == (
        "Scheduled for Sat Nov 14. First try Sat Oct 24, 11:50 PM. "
        "I'll message you how it goes."
    )
    already = {
        "next_target_date": "2026-10-24",
        "first_attempt_at": "2026-10-01T06:00:00-07:00",
    }
    assert "bookable already" in app_module.scheduled_status(already)


def test_a_day_not_open_yet_reads_plainly():
    started = StartedRun("r", "saticoy", "2026-11-14", dry_run=False)
    text = format_outcome(started, {"status": "not_open"}, "Saticoy")
    assert text.startswith("📭 **Not open yet:** Saticoy on Sat Nov 14")


NO_SLOT = (
    "ValueError: No tee times available between 09:00 AM and 11:00 AM. "
    "Try adjusting the time window."
)
PLAN_REQUEST = {
    "id": "req-1",
    "plan_id": "plan-1",
    "course": "soule-park",
    "target_date": "2026-11-14",
    "earliest": "09:00:00",
    "latest": "11:00:00",
    "players": 4,
    "holes": 18,
}


def _scheduled(status, claude=None, allowed=frozenset({42, 43})):
    """One scheduler tick that starts a run, followed to its `status`; a
    failed run is put to `claude`, if given."""
    claimed = SimpleNamespace(
        run_id="run-1",
        course=SimpleNamespace(key="soule-park"),
        criteria=SimpleNamespace(target_date=date(2026, 11, 14)),
    )
    run = {
        "status": status,
        "tee_time_local": None,
        "scheduled": True,
        "request_id": "req-1",
        "detail": NO_SLOT if status == "error" else None,
    }
    tools = FakeTools(
        {
            "get_course": ToolResult(json.dumps({"name": "Soule Park"})),
            "get_run": ToolResult(json.dumps(run)),
            "get_request": ToolResult(json.dumps(PLAN_REQUEST)),
        }
    )
    settings = replace(BOT_SETTINGS, allowed_user_ids=allowed)
    agent = _agent(claude, tools) if claude is not None else None
    bot = BookingBot(agent, tools, None, settings, pays=True)
    telegram = FlakyTelegram()

    async def go():
        await bot.schedule_once(telegram, lambda: [Started(claimed, dry_run=False)])
        await asyncio.gather(*bot._watches)

    asyncio.run(go())
    return telegram


def test_a_scheduled_runs_outcome_goes_to_every_allowed_user(no_sleep):
    telegram = _scheduled("success")
    assert [kwargs["chat_id"] for _, kwargs in telegram.requests] == [42, 43]
    assert all(text.startswith("⏰ **Scheduled booking**") for text in telegram.sent)
    assert "Booked" in telegram.sent[0]


def test_a_scheduled_try_that_finds_the_day_shut_says_nothing(no_sleep):
    assert _scheduled("not_open").sent == []


def test_a_failed_run_is_explained_by_the_model_not_sent_raw(no_sleep):
    claude = FakeClaude(
        ([_text("Nothing open 9–11 AM at Soule Park on **Sat Nov 14**.")], "end_turn")
    )
    telegram = _scheduled("error", claude, allowed=frozenset({42}))

    assert telegram.sent == ["Nothing open 9–11 AM at Soule Park on **Sat Nov 14**."]
    note = claude.requests[0]["messages"][-1]["content"]
    assert "scheduled booking at Soule Park (soule-park) on 2026-11-14" in note
    assert "No tee times available between 09:00 AM and 11:00 AM" in note
    assert "request req-1" in note
    # Enough for the follow-up: what was asked for, and that the plan carries on.
    assert "09:00-11:00, 4 players, 18 holes" in note
    assert "weekly plan plan-1, which stays scheduled" in note


def test_a_failed_run_still_reaches_the_person_when_claude_is_down(no_sleep):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    claude = FakeClaude(anthropic.APIConnectionError(request=request))
    telegram = _scheduled("error", claude, allowed=frozenset({42}))

    [text] = telegram.sent
    assert text.startswith("⏰ **Scheduled booking**\n\n❌ **The run failed**")
    assert "No tee times available" in text


def test_a_failed_scheduler_tick_is_logged_not_raised():
    bot = BookingBot(None, FakeTools(), None, BOT_SETTINGS, pays=True)

    def tick():
        raise RuntimeError("database is locked")

    asyncio.run(bot.schedule_once(FlakyTelegram(), tick))  # no raise
    assert not bot._watches
