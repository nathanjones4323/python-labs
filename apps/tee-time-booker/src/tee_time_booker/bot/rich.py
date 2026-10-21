"""Telegram rich messages: formatted replies and a live progress draft.

Rich messages (Bot API 10.1+) take GitHub-flavoured Markdown, which may also
carry some HTML (`<footer>`, `<blockquote expandable>`). python-telegram-bot
has no wrappers for them yet, so these go through `Bot.do_api_request`.

A draft (`sendRichMessageDraft`) is an ephemeral preview that lasts about 30
seconds; `Progress` keeps one up while the model works and says which step
it's on.
"""

import asyncio
import re
from collections.abc import Sequence
from typing import Any

from loguru import logger
from telegram.error import TelegramError

# Backslash-escaped in values put into a template: Markdown punctuation, the
# HTML brackets and `$`, which opens a formula.
_SPECIAL = re.compile(r"([\\`*_~|\[\]()<>#=+\-!$])")
# A `$` before a digit is a price, never a formula ("$20 or $25" would be one).
_PRICE = re.compile(r"(?<!\\)\$(?=\d)")
# A draft lasts about 30 seconds; refresh it well before then.
DRAFT_REFRESH_S = 20

STEP_LABELS = {
    "find_tee_times": "Checking what's open…",
    "show_tee_times": "Laying out the times…",
    "list_plans": "Looking at your plans…",
    "get_plan": "Looking at your plans…",
    "list_courses": "Looking up courses…",
    "get_course": "Looking up courses…",
    "create_request": "Setting up the request…",
    "list_requests": "Looking at your requests…",
    "get_request": "Looking at your requests…",
    "cancel_request": "Cancelling the request…",
    "book_plan": "Starting the run…",
    "run_request": "Starting the run…",
    "get_run": "Checking your runs…",
    "list_runs": "Checking your runs…",
    "list_request_runs": "Checking your runs…",
}


def escape(text: Any) -> str:
    """`text` shown literally inside rich Markdown."""
    return _SPECIAL.sub(r"\\\1", str(text))


def escape_prices(markdown: str) -> str:
    """The model's Markdown with its prices kept out of formulas. Not for
    templates: inside an HTML block a backslash shows as itself."""
    return _PRICE.sub(r"\\$", markdown)


async def send_rich(bot: Any, chat_id: int, markdown: str, **kwargs: Any) -> None:
    await bot.do_api_request(
        "sendRichMessage",
        api_kwargs={
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
            **kwargs,
        },
    )


async def edit_rich(bot: Any, chat_id: int, message_id: int, markdown: str) -> None:
    """Replace a message's content; any buttons on it are dropped."""
    await bot.do_api_request(
        "editMessageText",
        api_kwargs={
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown},
        },
    )


def step_label(calls: Sequence[Any]) -> str:
    labels = {STEP_LABELS.get(call.name, "Working on it…") for call in calls}
    return labels.pop() if len(labels) == 1 else "Working on it…"


class Progress:
    """A draft saying what the bot is doing, kept up until the block exits.

    Purely a courtesy: a draft that fails to send is logged and forgotten.
    """

    def __init__(self, bot: Any, chat_id: int, draft_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._draft_id = draft_id
        self._label = "Thinking…"
        self._refresher: asyncio.Task | None = None

    async def __aenter__(self) -> "Progress":
        await self._send()
        self._refresher = asyncio.create_task(self._refresh())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._refresher.cancel()
        await asyncio.gather(self._refresher, return_exceptions=True)

    async def step(self, calls: Sequence[Any]) -> None:
        """The model is about to run these tool calls."""
        self._label = step_label(calls)
        await self._send()

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(DRAFT_REFRESH_S)
            await self._send()

    async def _send(self) -> None:
        try:
            await self._bot.do_api_request(
                "sendRichMessageDraft",
                api_kwargs={
                    "chat_id": self._chat_id,
                    "draft_id": self._draft_id,
                    "rich_message": {
                        "markdown": f"<tg-thinking>{self._label}</tg-thinking>"
                    },
                },
            )
        except TelegramError as e:
            logger.warning(f"Progress draft for chat {self._chat_id} failed: {e}")
