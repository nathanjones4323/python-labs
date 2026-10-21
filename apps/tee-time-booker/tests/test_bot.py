"""The Telegram bot, without Telegram or Claude: the tool allowlist, the
payment gate, the conversation's bookkeeping and the run messages."""

import asyncio
import json
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
from tee_time_booker.bot.agent import BookingAgent, StartedRun  # noqa: E402
from tee_time_booker.bot.app import (  # noqa: E402
    BookingBot,
    format_outcome,
    format_runs,
    send_with_retry,
)
from tee_time_booker.bot.rich import Progress, escape  # noqa: E402
from tee_time_booker.bot.tools import ALLOWED_TOOLS, BookingTools, ToolResult  # noqa: E402
from tee_time_booker.config import BotSettings, Settings  # noqa: E402
from tee_time_booker.mcp import create_server  # noqa: E402
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
    asyncio.run(send_with_retry(telegram, 7, "**Booked** for $20", reply_markup="kb"))
    [(endpoint, kwargs)] = telegram.requests
    assert endpoint == "sendRichMessage"
    # A price's `$` would otherwise open a formula.
    assert kwargs == {
        "chat_id": 7,
        "rich_message": {"markdown": r"**Booked** for \$20"},
        "reply_markup": "kb",
    }


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
    assert "create_plan" not in names
    assert {p["name"] for p in plans} == {"soule-saturday", "saticoy-saturday"}
    assert refused.is_error
