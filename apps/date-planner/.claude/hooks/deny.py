"""PreToolUse hook that refuses the call, with the reason given as argv[1].

    uv run python .claude/hooks/deny.py 'Date Planner never sends email.'

Reads (and ignores) the payload on stdin so Claude Code's pipe is drained.
"""

import json
import sys


def main() -> None:
    reason = sys.argv[1] if len(sys.argv) > 1 else "Blocked by a Date Planner hook."
    sys.stdin.read()
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
