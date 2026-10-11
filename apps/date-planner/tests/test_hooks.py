"""The Claude Code hooks in `.claude/hooks`, fed sample PreToolUse payloads.

Run as Claude Code runs them — a subprocess with the payload on stdin — so
the stdout JSON is checked exactly as the harness will read it.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"
SETTINGS = HOOKS.parent / "settings.json"
CALENDAR = "mcp__claude_ai_Google_Calendar__"


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


def call(tool: str, **tool_input: Any) -> dict[str, Any]:
    return {
        "session_id": "abc123",
        "hook_event_name": "PreToolUse",
        "tool_name": CALENDAR + tool,
        "tool_input": tool_input,
        "tool_use_id": "toolu_01",
    }


@pytest.mark.parametrize(
    ("payload", "decision"),
    [
        (call("delete_event", eventId="evt_1"), "deny"),
        (
            call(
                "create_event",
                summary="[TENTATIVE] Hike",
                startTime="2026-10-24T10:20:00",
                endTime="2026-10-24T12:05:00",
            ),
            "allow",
        ),
        (
            call(
                "create_event",
                summary="Dinner",
                startTime="2026-10-24T17:45:00",
                endTime="2026-10-24T19:15:00",
                attendees=[{"email": "partner@example.com"}],
            ),
            "ask",
        ),
        (call("create_event", summary="x", attendeeEmails=["p@example.com"]), "ask"),
        (
            call("update_event", eventId="e", addedAttendees=[{"email": "p@x.com"}]),
            "ask",
        ),
        (call("update_event", eventId="e", guests=["p@example.com"]), "ask"),
        # Empty lists and guest *permissions* invite nobody.
        (call("create_event", summary="x", attendees=[]), "allow"),
        (
            call(
                "update_event", eventId="e", guestPermissions={"guestsCanModify": False}
            ),
            "allow",
        ),
        (call("update_event", eventId="e", removedAttendeeEmails=["p@x.com"]), "allow"),
        (call("update_event", eventId="e", summary="[CANCELLED] Dinner"), "allow"),
    ],
    ids=lambda v: v if isinstance(v, str) else v["tool_name"].rsplit("__")[-1],
)
def test_calendar_guard(payload: dict[str, Any], decision: str) -> None:
    output = run_hook("calendar_guard.py", payload)
    assert output["permissionDecision"] == decision
    if decision == "deny":
        assert "retitle as [CANCELLED]" in output["permissionDecisionReason"]


def test_calendar_guard_fails_closed_on_garbage() -> None:
    assert run_hook("calendar_guard.py", "not json")["permissionDecision"] == "deny"
    assert run_hook("calendar_guard.py", "[]")["permissionDecision"] == "deny"


def test_deny_hook_uses_its_reason() -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__claude_ai_Gmail__send_message",
        "tool_input": {"to": ["x@example.com"]},
    }
    output = run_hook("deny.py", payload, "Date Planner never sends email.")
    assert output == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Date Planner never sends email.",
    }


@pytest.mark.parametrize(
    ("tool", "hook"),
    [
        (CALENDAR + "delete_event", "calendar_guard.py"),
        (CALENDAR + "create_event", "calendar_guard.py"),
        (CALENDAR + "update_event", "calendar_guard.py"),
        (CALENDAR + "list_events", None),
        ("mcp__claude_ai_Gmail__send_message", "deny.py"),
        ("mcp__claude_ai_Gmail__reply", "deny.py"),
        ("mcp__claude_ai_Gmail__forward", "deny.py"),
        ("mcp__claude_ai_Gmail__search_threads", None),
        ("mcp__claude_ai_Gmail__create_draft", None),
    ],
)
def test_settings_matchers_hit_the_connectors_tool_names(
    tool: str, hook: str | None
) -> None:
    """The real connector names, as listed in Claude Code (see README)."""
    import re

    entries = json.loads(SETTINGS.read_text())["hooks"]["PreToolUse"]
    # Claude Code treats these matchers as unanchored JS regexes; Python's
    # `re.search` agrees with JS on everything these patterns use.
    hit = [
        entry["hooks"][0]["command"]
        for entry in entries
        if re.search(entry["matcher"], tool)
    ]
    if hook is None:
        assert hit == []
    else:
        assert len(hit) == 1 and hook in hit[0]
