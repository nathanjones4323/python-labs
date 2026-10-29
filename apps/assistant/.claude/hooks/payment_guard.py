"""PreToolUse guard: money moves only on the person's own tap.

The assistant runs in bypassPermissions mode, so nothing asks. Paying is the
one exception, and it works the way the apps' own bots do: the app draws a
Confirm (tee times) or Pay (campsites) button, and only a tap pays.

Claude Code runs this before every tee-time-booker and campsite-booker tool
(see `.claude/settings.json`) with the call as JSON on stdin:

* A call that would pay directly (tee-time `run_request` / `book_plan` with
  `submit_payment`, `create_plan` / `create_request` with `auto_book`,
  `set_plan_auto_book` with `enabled`; campsite `hold_campsite` with `pay`,
  `pay_hold`, `set_watch_auto_pay` turning it on) → deny, naming the card
  tool that draws the button instead.
* `press_button` with a paying button (`tee:pay:…`, `camp:pay:…`,
  `camp:autopay:…`) → allowed only when the turn was started by the person
  tapping that very button: the transcript's last prompt is a Telegram
  channel message whose `button` attribute is exactly this data. The plugin
  only delivers taps from allowlisted senders.
* A shell command that reaches the booking apps' code, settings or payment
  switches (their CLIs book and pay without any card) → deny.
* Anything else → no output, so the call runs as usual.

Stdlib only. A payload it can't read, for a tool it guards, is denied: a
guard that fails open isn't one.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

CHANNEL_SERVER = "plugin:telegram:telegram"
# The opening <channel …> tag of a prompt, and the tap's button data in it.
_TAG = re.compile(r"^\s*<channel\b[^>]*>")
_BUTTON = re.compile(r'\bbutton="([^"]*)"')

# tool -> the argument that makes it pay.
TEE_PAYING = {
    "book_plan": "submit_payment",
    "run_request": "submit_payment",
    "create_plan": "auto_book",
    "create_request": "auto_book",
    "set_plan_auto_book": "enabled",
}
CAMP_PAYING = {"hold_campsite": "pay", "set_watch_auto_pay": "on"}
PAYING_BUTTONS = ("tee:pay:", "camp:pay:", "camp:autopay:")
# The booking apps from the shell: their CLIs, modules, .env files and the
# switches that turn payment on.
_BOOKING_SHELL = re.compile(
    r"tee[-_]time[-_]booker|campsite[-_]booker|SUBMIT_PAYMENT|CREDIT_CARD",
    re.IGNORECASE,
)

TEE_REASON = (
    "Paying goes through the person's tap: call ask_to_pay with this tool and "
    "its arguments, send the Confirm card it returns, and stop."
)
CAMP_REASON = (
    "Paying goes through the person's tap: hold without pay and send the hold "
    "card (its Pay button pays), or call ask_auto_pay for a watch's auto-pay."
)
SHELL_REASON = (
    "The booking apps run through their MCP tools only, never the shell: "
    "their CLIs can book and pay without the person's tap."
)
TAP_REASON = (
    "Only the person's own tap of this button can pay. Send the card and "
    "wait for them to tap it."
)


def turn_button(transcript: str) -> str | None:
    """The button data of the Telegram tap that started this turn, if one did."""
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
    tag = _TAG.match(content) if isinstance(content, str) else None
    button = _BUTTON.search(tag.group(0)) if tag else None
    return button.group(1) if button else None


def pays(flag: str, tool_input: dict[str, Any], default: bool = False) -> bool:
    # Anything but an absent or literal False counts: the apps coerce "true"
    # or 1 to True.
    return tool_input.get(flag, default) is not False


def decide(payload: dict[str, Any], transcript: str) -> tuple[str, str] | None:
    """("deny", reason), or None to let the call run."""
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "deny", "Payment guard couldn't read the tool input."
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or _BOOKING_SHELL.search(command):
            return "deny", SHELL_REASON
        return None
    _, server, name = (tool.split("__", 2) + ["", ""])[:3]
    if server == "tee-time-booker" and name in TEE_PAYING:
        if pays(TEE_PAYING[name], tool_input):
            return "deny", TEE_REASON
    elif server == "campsite-booker" and name == "pay_hold":
        return "deny", CAMP_REASON
    elif server == "campsite-booker" and name in CAMP_PAYING:
        # set_watch_auto_pay's `on` defaults to True.
        if pays(CAMP_PAYING[name], tool_input, default=name == "set_watch_auto_pay"):
            return "deny", CAMP_REASON
    elif name == "press_button":
        data = tool_input.get("data")
        if not isinstance(data, str):
            return "deny", "Payment guard found no button data."
        if data.startswith(PAYING_BUTTONS) and turn_button(transcript) != data:
            return "deny", TAP_REASON
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        path = payload.get("transcript_path")
        transcript = Path(path).read_text() if isinstance(path, str) else ""
        decision = decide(payload, transcript)
    except (ValueError, OSError) as exc:  # json.JSONDecodeError is a ValueError
        decision = ("deny", f"Payment guard couldn't parse the call: {exc}")
    if decision is None:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision[0],
                "permissionDecisionReason": decision[1],
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
