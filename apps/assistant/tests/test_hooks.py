"""The assistant's Claude Code hooks and settings, fed sample payloads.

Run as Claude Code runs them — a subprocess with the payload on stdin — so
the stdout JSON is checked exactly as the harness will read it.

    uvx pytest apps/assistant/tests
"""

import http.server
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

PROJECT = Path(__file__).resolve().parent.parent
HOOKS = PROJECT / ".claude" / "hooks"
SETTINGS = json.loads((PROJECT / ".claude" / "settings.json").read_text())
MCP = json.loads((PROJECT / ".mcp.json").read_text())


def run_hook(script: str, payload: Any, *args: str) -> dict[str, Any]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    done = subprocess.run(
        [sys.executable, str(HOOKS / script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    output: dict[str, Any] = json.loads(done.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    return output


def write(file_path: str, tool: str = "Write") -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": {"file_path": file_path}}


@pytest.mark.parametrize(
    "path",
    [
        str(PROJECT / "memory" / "USER.md"),
        "memory/journal/2026-10-23.md",
    ],
)
def test_memory_writes_are_allowed(path: str) -> None:
    out = run_hook("memory_only.py", write(path, "Edit"), str(PROJECT))
    assert out["permissionDecision"] == "allow"


@pytest.mark.parametrize(
    "path",
    [
        str(PROJECT / "CLAUDE.md"),
        str(PROJECT / ".claude" / "settings.json"),
        "memory/../CLAUDE.md",
        str(PROJECT / "memoryx" / "USER.md"),
        str(Path.home() / ".zshrc"),
    ],
)
def test_writes_outside_memory_are_denied(path: str) -> None:
    out = run_hook("memory_only.py", write(path), str(PROJECT))
    assert out["permissionDecision"] == "deny"


def test_symlink_out_of_memory_is_denied(tmp_path: Path) -> None:
    project = tmp_path / "assistant"
    (project / "memory").mkdir(parents=True)
    (project / "memory" / "escape").symlink_to(tmp_path)
    target = str(project / "memory" / "escape" / "x.md")
    out = run_hook("memory_only.py", write(target), str(project))
    assert out["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "payload", ["not json", "[]", {"tool_name": "Write"}, {"tool_input": "x"}]
)
def test_unreadable_payload_is_denied(payload: Any) -> None:
    out = run_hook("memory_only.py", payload, str(PROJECT))
    assert out["permissionDecision"] == "deny"


def test_nothing_asks_and_email_and_event_deletes_are_blocked() -> None:
    permissions = SETTINGS["permissions"]
    assert permissions["defaultMode"] == "bypassPermissions"
    assert "ask" not in permissions
    assert set(permissions["deny"]) == {
        "mcp__claude_ai_Google_Calendar__delete_event",
        "mcp__claude_ai_Gmail__send_message",
        "mcp__claude_ai_Gmail__reply",
        "mcp__claude_ai_Gmail__forward",
    }


def test_tee_times_get_the_bots_cards() -> None:
    assert "--cards" in MCP["mcpServers"]["tee-time-booker"]["args"]


# --- telegram_progress.py ---------------------------------------------------

CHAT = "111222333"
TG_TOOL = "mcp__tee-time-booker__find_tee_times"


def load_progress() -> Any:
    spec = importlib.util.spec_from_file_location(
        "telegram_progress", HOOKS / "telegram_progress.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


progress = load_progress()



def channel_prompt(chat_id: str = CHAT) -> dict[str, Any]:
    """A Telegram DM as a real `--channels` transcript records it."""
    return {
        "type": "user",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "plugin:telegram:telegram"},
        "message": {
            "role": "user",
            "content": (
                f'<channel source="plugin:telegram:telegram" chat_id="{chat_id}" '
                f'message_id="8" user="nj" user_id="{chat_id}" '
                'ts="2026-10-25T05:39:46.000Z">\n'
                "any tee times Saturday morning?\n</channel>"
            ),
        },
    }


TERMINAL_PROMPT = {
    "type": "user",
    "origin": {"kind": "human"},
    "message": {"role": "user", "content": "any tee times Saturday morning?"},
}
TOOL_RESULT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
}


def transcript(*entries: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in entries) + "\n"


def run_silent_hook(
    script: str, stdin: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """A hook that must print nothing and exit 0, whatever it's fed."""
    done = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr == ""
    return done


class Capture(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, Any]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Capture.requests.append((self.path, json.loads(body)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def bot(tmp_path: Path) -> Any:
    """A state dir with a token and allowlist, and a fake Telegram API."""
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text("TELEGRAM_BOT_TOKEN=123:secret\n")
    (state / "access.json").write_text(json.dumps({"allowFrom": [CHAT]}))
    server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Capture.requests = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env |= {
        "TELEGRAM_STATE_DIR": str(state),
        "TELEGRAM_API_URL": f"http://127.0.0.1:{server.server_port}",
    }

    class Bot:
        def __init__(self) -> None:
            self.state, self.env, self.requests = state, env, Capture.requests

        def run(self, tool: str, entries: list[dict[str, Any]]) -> None:
            path = tmp_path / "t.jsonl"
            path.write_text(transcript(*entries))
            payload = {"tool_name": tool, "transcript_path": str(path)}
            run_silent_hook("telegram_progress.py", json.dumps(payload), env)

    yield Bot()
    server.shutdown()


def test_telegram_turn_sends_a_draft(bot: Any) -> None:
    bot.run(TG_TOOL, [TERMINAL_PROMPT, channel_prompt(), TOOL_RESULT])
    assert bot.requests == [
        (
            "/bot123:secret/sendRichMessageDraft",
            {
                "chat_id": int(CHAT),
                "draft_id": progress.DRAFT_ID,
                "rich_message": {
                    "markdown": "<tg-thinking>Checking what's open…</tg-thinking>"
                },
            },
        )
    ]


def test_terminal_turn_sends_nothing(bot: Any) -> None:
    bot.run(TG_TOOL, [channel_prompt(), TOOL_RESULT, TERMINAL_PROMPT, TOOL_RESULT])
    assert bot.requests == []


def test_plugin_tools_send_nothing(bot: Any) -> None:
    for tool in ("reply", "react", "edit_message", "download_attachment"):
        bot.run(f"mcp__plugin_telegram_telegram__{tool}", [channel_prompt()])
    assert bot.requests == []


def test_chat_outside_allowlist_sends_nothing(bot: Any) -> None:
    bot.run(TG_TOOL, [channel_prompt("999")])
    assert bot.requests == []


def test_missing_env_file_is_silent(bot: Any) -> None:
    (bot.state / ".env").unlink()
    bot.run(TG_TOOL, [channel_prompt()])
    assert bot.requests == []


def test_unreachable_api_is_silent(bot: Any) -> None:
    bot.env["TELEGRAM_API_URL"] = "http://127.0.0.1:9"
    bot.run(TG_TOOL, [channel_prompt()])


@pytest.mark.parametrize(
    "stdin",
    ["", "not json", "[]", "{}", '{"tool_name": "Read", "transcript_path": "/nope"}'],
)
def test_junk_stdin_is_silent(bot: Any, stdin: str) -> None:
    run_silent_hook("telegram_progress.py", stdin, bot.env)
    assert bot.requests == []


@pytest.mark.parametrize(
    "lines", ["", "not json\n{]\n", '{"type": "user", "origin": "x"}\n', "[1]\n"]
)
def test_bad_transcript_is_silent(bot: Any, tmp_path: Path, lines: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(lines)
    payload = {"tool_name": TG_TOOL, "transcript_path": str(path)}
    run_silent_hook("telegram_progress.py", json.dumps(payload), bot.env)
    assert bot.requests == []


@pytest.mark.parametrize(
    ("tool", "label"),
    [
        ("mcp__tee-time-booker__search_tee_times", "Checking what's open…"),
        ("mcp__tee-time-booker__list_plans", "Looking at your plans…"),
        ("mcp__garmin__get_sleep_data", "Checking Garmin…"),
        ("mcp__date-planner__get_sunset", "Working on the date…"),
        ("mcp__claude_ai_Google_Calendar__list_events", "Checking your calendar…"),
        ("mcp__claude_ai_Gmail__search_threads", "Checking your email…"),
        ("Edit", "Updating your notes…"),
        ("mcp__strava-mcp__list_activities", "Working on it…"),
        ("ToolSearch", "Working on it…"),
    ],
)
def test_labels(tool: str, label: str) -> None:
    found = progress.target(
        {"tool_name": tool}, transcript(channel_prompt()), [int(CHAT)]
    )
    assert found == (CHAT, label)


def test_target_needs_a_telegram_turn() -> None:
    call = {"tool_name": TG_TOOL}
    assert progress.target(call, transcript(TERMINAL_PROMPT), [CHAT]) is None
    assert progress.target(call, transcript(channel_prompt()), []) is None
    assert progress.target({}, transcript(channel_prompt()), [CHAT]) is None


def test_button_tap_turn_targets_its_chat() -> None:
    # The forked plugin delivers a tap as a channel message with `button`.
    tap = channel_prompt()
    tap["message"]["content"] = tap["message"]["content"].replace(
        'message_id="8"', 'message_id="8" button="tee:soule-park:2026-11-01T07:10:4:18"'
    )
    found = progress.target({"tool_name": "mcp__tee-time-booker__run_request"}, transcript(tap), [CHAT])
    assert found == (CHAT, "Starting the run…")


def test_progress_hook_runs_on_every_tool() -> None:
    group = next(
        g
        for g in SETTINGS["hooks"]["PreToolUse"]
        if "telegram_progress.py" in g["hooks"][0]["command"]
    )
    assert group["matcher"] == ".*"
    assert group["hooks"][0]["timeout"] == 3


def test_memory_guard_is_unchanged() -> None:
    assert SETTINGS["hooks"]["PreToolUse"][0] == {
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
            {
                "type": "command",
                "command": 'python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/'
                'memory_only.py" "${CLAUDE_PROJECT_DIR}"',
            }
        ],
    }


# --- payment_guard.py ---------------------------------------------------------


def tap(data: str) -> dict[str, Any]:
    entry = channel_prompt()
    entry["message"]["content"] = entry["message"]["content"].replace(
        'message_id="8"', f'message_id="8" button="{data}"'
    )
    return entry


def guard(
    tool: str, tool_input: Any, entries: list[dict[str, Any]], tmp_path: Path
) -> dict[str, Any] | None:
    """The guard's decision, or None when it lets the call through."""
    path = tmp_path / "t.jsonl"
    path.write_text(transcript(*entries))
    payload = {"tool_name": tool, "tool_input": tool_input, "transcript_path": str(path)}
    done = subprocess.run(
        [sys.executable, str(HOOKS / "payment_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    if not done.stdout:
        return None
    output: dict[str, Any] = json.loads(done.stdout)["hookSpecificOutput"]
    return output


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("mcp__tee-time-booker__run_request", {"request_id": "r", "submit_payment": True}),
        ("mcp__tee-time-booker__book_plan", {"plan_id": "p", "submit_payment": "true"}),
        ("mcp__tee-time-booker__create_plan", {"auto_book": True}),
        ("mcp__tee-time-booker__create_request", {"auto_book": 1}),
        ("mcp__tee-time-booker__set_plan_auto_book", {"plan_id": "p", "enabled": True}),
        ("mcp__campsite-booker__hold_campsite", {"pay": True}),
        ("mcp__campsite-booker__pay_hold", {"hold_id": 3}),
        ("mcp__campsite-booker__set_watch_auto_pay", {"watch_id": 3}),
        ("Bash", {"command": "uv run --package tee-time-booker tee-time-booker"}),
        ("Bash", {"command": "TEE_TIME_SUBMIT_PAYMENT=true python -m x"}),
        ("Bash", {"command": "cat ../campsite-booker/.env"}),
        ("Bash", {"command": "cd ../tee*; cat .env"}),
        ("Bash", {"command": "sed -i '' /payment/d .claude/settings.json"}),
        ("Bash", {"command": "cd ../pay-my-bills && uv run pay-my-bills"}),
    ],
)
def test_paying_directly_is_denied(tool: str, tool_input: Any, tmp_path: Path) -> None:
    # Even on a tap's turn: paying only goes through press_button.
    out = guard(tool, tool_input, [tap("tee:pay:x")], tmp_path)
    assert out is not None and out["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    ("command", "says"),
    [
        ("uv run --package tee-time-booker tee-time-booker", "booking apps"),
        ("grep -n '^[A-Z_]*=' ../pay-my-bills/.env", "pay-my-bills"),
        ("sqlite3 ~/.local/share/pay-my-bills/bills.db .tables", "pay-my-bills"),
        ("cat ../meal-planner/.env", ".env files"),
    ],
)
def test_a_shell_denial_names_its_reason(command: str, says: str, tmp_path: Path) -> None:
    out = guard("Bash", {"command": command}, [TERMINAL_PROMPT], tmp_path)
    assert out is not None and says in out["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("mcp__pay-my-bills__bill_status", {}),
        ("mcp__tee-time-booker__run_request", {"request_id": "r", "submit_payment": False}),
        ("mcp__tee-time-booker__create_request", {"course": "x"}),
        ("mcp__tee-time-booker__delete_plan", {"plan_id": "p"}),
        ("mcp__tee-time-booker__search_tee_times", {"date": "2026-10-31"}),
        ("mcp__campsite-booker__hold_campsite", {"campsite_id": "10"}),
        ("mcp__campsite-booker__set_watch_auto_pay", {"watch_id": 3, "on": False}),
        ("mcp__tee-time-booker__press_button", {"data": "tee:book:abc:0"}),
        ("mcp__tee-time-booker__press_button", {"data": "tee:dry:abc"}),
        ("mcp__campsite-booker__press_button", {"data": "camp:hold:1"}),
        ("Bash", {"command": "ls ~/Downloads && git status"}),
    ],
)
def test_everything_else_runs(tool: str, tool_input: Any, tmp_path: Path) -> None:
    assert guard(tool, tool_input, [TERMINAL_PROMPT], tmp_path) is None


@pytest.mark.parametrize("data", ["tee:pay:abc", "camp:pay:3", "camp:autopay:3"])
def test_a_paying_button_needs_that_very_tap(data: str, tmp_path: Path) -> None:
    tool = "mcp__x__press_button"
    call = {"data": data}
    assert guard(tool, call, [tap(data)], tmp_path) is None
    # The tool result that follows a tap doesn't change whose turn it is.
    assert guard(tool, call, [tap(data), TOOL_RESULT], tmp_path) is None
    for entries in (
        [TERMINAL_PROMPT],
        [channel_prompt()],
        [tap(data + "0")],
        [tap("tee:book:abc:0")],
        [tap(data), TERMINAL_PROMPT],
        # An interrupt or slash command has no origin but still ends the turn.
        [tap(data), {"type": "user", "message": {"content": "[Request interrupted by user]"}}],
        [tap(data), {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}],
    ):
        out = guard(tool, call, entries, tmp_path)
        assert out is not None and out["permissionDecision"] == "deny", entries


@pytest.mark.parametrize("payload", ["not json", "[]"])
def test_guard_fails_closed(payload: str) -> None:
    out = run_hook("payment_guard.py", payload)
    assert out["permissionDecision"] == "deny"


def test_guard_runs_on_the_booking_apps_and_the_shell() -> None:
    group = next(
        g
        for g in SETTINGS["hooks"]["PreToolUse"]
        if "payment_guard.py" in g["hooks"][0]["command"]
    )
    for tool in ("Bash", "mcp__tee-time-booker__press_button", "mcp__campsite-booker__pay_hold"):
        assert re.fullmatch(group["matcher"], tool)
    assert not re.fullmatch(group["matcher"], "mcp__garmin__get_stats")
