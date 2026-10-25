"""The assistant's Claude Code hooks and settings, fed sample payloads.

Run as Claude Code runs them — a subprocess with the payload on stdin — so
the stdout JSON is checked exactly as the harness will read it.

    uvx pytest apps/assistant/tests
"""

import http.server
import importlib.util
import json
import os
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


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__tee-time-booker__delete_plan",
        "mcp__claude_ai_Gmail__send_message",
        "mcp__claude_ai_Gmail__reply",
        "mcp__claude_ai_Gmail__forward",
        "Bash",
    ],
)
def test_high_impact_tools_are_denied(tool: str) -> None:
    assert tool in SETTINGS["permissions"]["deny"]
    assert tool not in SETTINGS["permissions"]["allow"]


@pytest.mark.parametrize(
    "tool", ["mcp__tee-time-booker__book_plan", "mcp__tee-time-booker__run_request"]
)
def test_booking_always_asks(tool: str) -> None:
    # `ask` beats an `allow` rule, so a wildcard allow can't skip the prompt.
    assert tool in SETTINGS["permissions"]["ask"]
    assert tool not in SETTINGS["permissions"]["allow"]
    assert tool not in SETTINGS["permissions"]["deny"]


def test_every_allowed_mcp_tool_names_a_configured_server() -> None:
    servers = set(MCP["mcpServers"]) | {"claude_ai_Google_Calendar", "claude_ai_Gmail"}
    for rule in SETTINGS["permissions"]["allow"]:
        if rule.startswith("mcp__"):
            assert rule.split("__")[1] in servers, rule


def test_calendar_guard_path_exists() -> None:
    commands = [
        hook["command"]
        for group in SETTINGS["hooks"]["PreToolUse"]
        for hook in group["hooks"]
    ]
    guard = next(c for c in commands if "calendar_guard.py" in c)
    rel = guard.split('"${CLAUDE_PROJECT_DIR}/')[1].split('"')[0]
    assert (PROJECT / rel).is_file()


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
