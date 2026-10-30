# Target state

One Claude Code project, `apps/assistant`, that reaches every app through
its existing MCP server, talks over Telegram, and remembers things in
Markdown. It runs on the laptop under the Claude subscription.

## Why Claude Code, not OpenClaw

Since April 2026 a Claude subscription no longer covers third-party
harnesses like OpenClaw; they need an API key or Extra Usage. OpenClaw's
`claude-cli` backend runs on the subscription but is text-only (no tools),
so it can't call these MCPs. Claude Code already provides each piece the
plan wanted from OpenClaw:

| Plan component | Here |
|---|---|
| Agent runtime, sessions | a `claude` session started in `apps/assistant` |
| Mobile messaging | Telegram channel plugin, forked into `plugins/telegram` with inline buttons and served from the repo's own marketplace (`python-labs`); sender allowlist by pairing; permission prompts relayed to the chat |
| Scheduling, hooks | session-scheduled tasks in the always-on session; Claude Code hooks; launchd keeps the session up |
| MCP config | `apps/assistant/.mcp.json` |
| Memory | `apps/assistant/memory/*.md`, imported from `apps/assistant/CLAUDE.md` |
| Permissions, audit | bypass mode, `deny` rules, PreToolUse hooks (payment by tap only), each app's own switches |
| Structured task state | none until a workflow needs it (Phase 6) |

Remote Control stays as the partner's path into `apps/date-planner`.

## Ownership

- Apps own their data and rules. The assistant only calls their MCP tools;
  no gateway, no second agent runtime, no copies of domain logic in prompts.
- Each app's own safety stays authoritative (tee-time-booker's double
  payment gate, the calendar guard, pay-my-bills' single writer).
- Bots that run schedulers (tee-time-booker, pay-my-bills) keep running;
  the assistant reads their state, it doesn't replace them.

## Permissions

The session runs in `bypassPermissions` mode: nothing asks, over Telegram or
in the terminal. Paying is gated the way the apps' own bots gate it, by a
tap on a button the app draws.

| Rule | Where |
|---|---|
| Everything runs without a prompt | `permissions.defaultMode` |
| Paying only on the person's tap of that Confirm / Pay button | PreToolUse hook `payment_guard.py` |
| No shell route into the booking apps (CLIs, `.env`, payment switches) | `payment_guard.py` on `Bash` |
| Email send/reply/forward and calendar deletes blocked | `deny` rules |
| Writes only under `apps/assistant/memory/` | PreToolUse hook `memory_only.py` |

`payment_guard.py` denies every direct paying call (tee-time `run_request`
/ `book_plan` with `submit_payment`, `auto_book`, `set_plan_auto_book`;
campsite `hold_campsite` with `pay`, `pay_hold`, `set_watch_auto_pay`) and
lets `press_button` with a paying button (`tee:pay:`, `camp:pay:`,
`camp:autopay:`) through only when the turn's prompt is the Telegram tap of
exactly that button. Paying still also needs each app's own switch
(`TEE_TIME_SUBMIT_PAYMENT`, `CAMPSITE_SUBMIT_PAYMENT`); without it a Confirm
is a dry run.

## The apps' cards

The apps' MCP servers return their bots' own messages, so the assistant
shows and books exactly as the standalone bots do instead of redrawing them
from prompt text. A result's `telegram` field (`text`, `format`, `buttons`)
is sent as given; a tap's data goes back to that app's `press_button`.

- tee-time-booker, with `--cards` (`tee_time_booker/assistant.py`):
  `show_tee_times`, `ask_to_pay`, `press_button`, `wait_for_run`. The cards
  come from `tee_time_booker/cards.py`, which the bot uses too.
- campsite-booker: `find_open_campsites`, `hold_campsite` and `pay_hold`
  carry the watcher bot's cards (`campsite_booker/messages.py`);
  `press_button` and `ask_auto_pay` handle its buttons.

## Telegram buttons

The official plugin only draws buttons for permission prompts. The fork
(`plugins/telegram`, from v0.0.7; changes marked `Fork:` in `server.ts`)
adds an optional `buttons` arg to `reply` and `edit_message`: rows of
`{text, data}`, sent as callback data `btn:<data>`.

- A `btn:*` tap from an `allowFrom` sender is answered, the message is
  edited to show the choice (its buttons go, so it can't be answered twice),
  and Claude gets a `notifications/claude/channel` event with `meta.button`
  and `meta.message_id`. Others get "Not authorized." `perm:*` is unchanged.
- `reply` and `edit_message` default to `format: rich` (`sendRichMessage`,
  as the tee-time bot sends): Markdown plus `<table>`, `<footer>` and
  `<blockquote expandable>`, with `$`-prices escaped outside HTML blocks.
  A rich message comes back in a tap without its content, so the plugin
  keeps the Markdown of rich cards with buttons in memory and redraws the
  card with the choice in a `<footer>`; after a restart a tap only drops
  the buttons.
- A tap starts a turn like any message. A paying button's tap is the only
  thing that lets that payment through (`payment_guard.py`).
- Buttons are `{text, data}` or `{text, url}` (a link), and `format` may
  also be `html`, as the campsite bot sends.
- Tee times: the card's slot buttons lead to the bot's Confirm card
  (Confirm and pay / Dry run / Cancel), see "The apps' cards".
- Not on the channels allowlist, so the session runs with
  `--dangerously-load-development-channels plugin:telegram@python-labs`.
  The plugin keeps the name `telegram`: tool names, `/telegram:*` skills and
  `~/.claude/channels/telegram/` state are the same as the official one's.
  Install one or the other, never both (one poller per bot token).

## Memory

```
apps/assistant/memory/
  USER.md        stable preferences (milestone 1)
  PROJECTS.md    status and next steps          (milestone 2)
  ROUTINES.md    approved recurring procedures  (milestone 2)
  DECISIONS.md   dated decisions + rationale    (milestone 2)
  journal/YYYY-MM-DD.md  task outcomes          (milestone 2)
```

- Read before acting (`@memory/USER.md` in `CLAUDE.md`).
- Journal appends for outcomes; stable files change by one-line edits, only
  for confirmed facts, dated.
- No secrets. Writes are limited to `memory/` by hook. One always-on session
  is the single writer.
- History and rollback through git: memory is committed in the main
  checkout. Inspect, correct or delete it by editing the files.

## Runtime

- Spike: run by hand from a worktree (dev DBs).
- Installed: LaunchAgent running
  `claude --dangerously-load-development-channels plugin:telegram@python-labs`
  in the main checkout's `apps/assistant`, logs in `~/Library/Logs`, stopped
  with `launchctl bootout`. Its own Telegram bot — never reuse the
  pay-my-bills or tee-time-booker tokens (two pollers on one bot conflict).
