"""The assistant's Claude Code hooks and settings, fed sample payloads.

Run as Claude Code runs them — a subprocess with the payload on stdin — so
the stdout JSON is checked exactly as the harness will read it.

    uvx pytest apps/assistant/tests
"""

import json
import subprocess
import sys
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
        "mcp__tee-time-booker__book_plan",
        "mcp__tee-time-booker__run_request",
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
