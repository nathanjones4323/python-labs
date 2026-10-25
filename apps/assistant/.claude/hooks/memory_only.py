"""PreToolUse guard: the assistant may only write inside `memory/`.

Claude Code runs this before every `Edit`, `Write` and `NotebookEdit` (see
`.claude/settings.json`) with the call as JSON on stdin and the project dir
as argv[1]. A path that resolves inside `<project>/memory/` is allowed;
anything else, including `..` tricks and symlinks out, is denied.

Stdlib only. A payload it can't read is denied: a guard that fails open
isn't one.
"""

import json
import sys
from pathlib import Path
from typing import Any

REASON = "The assistant only writes its memory files (apps/assistant/memory/)."


def decide(payload: dict[str, Any], project_dir: Path) -> tuple[str, str]:
    """(permissionDecision, reason) for one PreToolUse payload."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "deny", "Memory guard couldn't read the tool input."
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return "deny", "Memory guard found no file path in the call."
    path = Path(raw)
    if not path.is_absolute():
        path = project_dir / path
    memory = (project_dir / "memory").resolve()
    if path.resolve().is_relative_to(memory):
        return "allow", "Inside memory/."
    return "deny", REASON


def main() -> None:
    project_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        decision, reason = decide(payload, project_dir)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        decision, reason = "deny", f"Memory guard couldn't parse the call: {exc}"
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
