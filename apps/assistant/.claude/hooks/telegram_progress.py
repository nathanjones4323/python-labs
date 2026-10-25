"""PreToolUse courtesy: show the Telegram sender which step the assistant is on.

Claude Code runs this before every tool call (see `.claude/settings.json`)
with the call as JSON on stdin. When the turn came from a Telegram DM, it
sends a rich-message draft ("Checking what's open…") to that chat, the way
the old tee-time bot did (`tee_time_booker/bot/rich.py`). A draft lasts
about 30 seconds, and a fixed draft_id makes each step replace the last.

The turn's origin comes from the transcript: the last entry with an
`origin` is the prompt that started this turn (tool results carry none),
and a channel prompt's text opens with
`<channel source="plugin:telegram:telegram" chat_id="…" …>`.

Stdlib only. It prints nothing, ever: any output, above all a
permissionDecision, would change how the call is handled. Every failure is
swallowed, and the bot token is never printed.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

API_URL = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")
TIMEOUT_S = 2
DRAFT_ID = 1
CHANNEL_SERVER = "plugin:telegram:telegram"
# The plugin's own tools: the reply itself, reactions, edits, downloads.
PLUGIN_PREFIX = "mcp__plugin_telegram_telegram__"
FALLBACK = "Working on it…"

# Copied from tee_time_booker/bot/rich.py, which this hook can't import.
STEP_LABELS = {
    "find_tee_times": "Checking what's open…",
    "search_tee_times": "Checking what's open…",
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
SERVER_LABELS = {
    "garmin": "Checking Garmin…",
    "date-planner": "Working on the date…",
    "claude_ai_Google_Calendar": "Checking your calendar…",
    "claude_ai_Gmail": "Checking your email…",
}
# Built-in tools. The assistant only writes its memory notes.
BUILTIN_LABELS = {
    "Edit": "Updating your notes…",
    "Write": "Updating your notes…",
    "WebSearch": "Searching the web…",
    "WebFetch": "Reading a web page…",
}

_CHAT_ID = re.compile(r'^\s*<channel\b[^>]*\bchat_id="(-?\d+)"')


def label(tool_name: str) -> str:
    """What the sender sees while `tool_name` runs."""
    if not tool_name.startswith("mcp__"):
        return BUILTIN_LABELS.get(tool_name, FALLBACK)
    server, _, tool = tool_name.removeprefix("mcp__").partition("__")
    return STEP_LABELS.get(tool) or SERVER_LABELS.get(server, FALLBACK)


def turn_chat_id(transcript: str) -> str | None:
    """The Telegram chat that started the current turn, if one did."""
    prompt: dict[str, Any] | None = None
    for line in transcript.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "user" and entry.get("origin"):
            prompt = entry
    if prompt is None or prompt["origin"] != {"kind": "channel", "server": CHANNEL_SERVER}:
        return None
    content = (prompt.get("message") or {}).get("content")
    if isinstance(content, list):  # a prompt sent as blocks
        content = next((b.get("text") for b in content if isinstance(b, dict)), None)
    match = _CHAT_ID.match(content) if isinstance(content, str) else None
    return match.group(1) if match else None


def target(
    payload: dict[str, Any], transcript: str, allow_from: list[str]
) -> tuple[str, str] | None:
    """(chat_id, label) to show for this call, or None to stay quiet."""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name.startswith(PLUGIN_PREFIX):
        return None
    chat_id = turn_chat_id(transcript)
    if chat_id is None or chat_id not in [str(a) for a in allow_from]:
        return None
    return chat_id, label(tool_name)


def state_dir() -> Path:
    """Where the plugin keeps access.json and .env (same lookup as its server)."""
    if os.environ.get("TELEGRAM_STATE_DIR"):
        return Path(os.environ["TELEGRAM_STATE_DIR"])
    config = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(config) / "channels" / "telegram"


def token(state: Path) -> str | None:
    """The bot token; a real environment variable wins over `.env`."""
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return os.environ["TELEGRAM_BOT_TOKEN"]
    for line in (state / ".env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "TELEGRAM_BOT_TOKEN":
            return value.strip()
    return None


def send(bot_token: str, chat_id: str, text: str) -> None:
    body = {
        "chat_id": int(chat_id),
        "draft_id": DRAFT_ID,
        "rich_message": {"markdown": f"<tg-thinking>{text}</tg-thinking>"},
    }
    request = urllib.request.Request(
        f"{API_URL}/bot{bot_token}/sendRichMessageDraft",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=TIMEOUT_S).close()


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or not isinstance(payload.get("transcript_path"), str):
        return
    state = state_dir()
    access = json.loads((state / "access.json").read_text())
    transcript = Path(payload["transcript_path"]).read_text()
    found = target(payload, transcript, access.get("allowFrom") or [])
    if found is None:
        return
    bot_token = token(state)
    if bot_token:
        send(bot_token, *found)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - a courtesy must never break a call
        pass
    sys.exit(0)
