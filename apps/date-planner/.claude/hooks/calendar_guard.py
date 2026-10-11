"""PreToolUse guard for the Google Calendar connector's write tools.

Claude Code runs this before any calendar `create_event`, `update_event` or
`delete_event` (see `.claude/settings.json`) and passes the call as JSON on
stdin: `{"tool_name": ..., "tool_input": {...}, ...}`. It prints a decision:

* `delete_event` → deny. Date Planner retitles events `[CANCELLED]` instead.
* `create_event` / `update_event` that adds anyone (any non-empty attendee
  or guest field) → ask, so the user approves every invite themselves.
* anything else → allow.

Stdlib only, so it runs before `uv sync` has ever happened. A payload it
can't read is denied: a guard that fails open isn't one.
"""

import json
import sys
from typing import Any

DELETE_REASON = "Date Planner never deletes events; retitle as [CANCELLED] instead."
INVITE_REASON = (
    "This calendar change invites someone. Date Planner only adds guests "
    "once you've approved it — check the attendees before allowing."
)

# The Google Calendar connector's names for "people to invite": `attendees`
# and the deprecated `attendeeEmails` on create_event, `addedAttendees` and
# `addedAttendeeEmails` on update_event. Anything with "attendee" or "guest"
# in its name counts, so a connector that calls them `guests` is covered too
# — except `guestPermissions`, which invites nobody.
NOT_AN_INVITE = frozenset({"guestpermissions", "removedattendeeemails"})


def _invites(tool_input: dict[str, Any]) -> list[str]:
    """The keys in `tool_input` that would add people to the event."""
    found = []
    for key, value in tool_input.items():
        lowered = key.lower()
        if lowered in NOT_AN_INVITE:
            continue
        if ("attendee" in lowered or "guest" in lowered) and value:
            found.append(key)
    return found


def decide(payload: dict[str, Any]) -> tuple[str, str]:
    """(permissionDecision, reason) for one PreToolUse payload."""
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "deny", "Calendar guard couldn't read the tool input."
    action = tool.rsplit("__", 1)[-1]
    if action.endswith("delete_event"):
        return "deny", DELETE_REASON
    if action.endswith(("create_event", "update_event")):
        keys = _invites(tool_input)
        if keys:
            return "ask", f"{INVITE_REASON} ({', '.join(keys)})"
    return "allow", "No deletion and no guests."


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        decision, reason = decide(payload)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        decision, reason = "deny", f"Calendar guard couldn't parse the call: {exc}"
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
