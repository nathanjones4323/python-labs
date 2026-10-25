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
| Permissions, audit | `.claude/settings.json` allow/ask/deny, PreToolUse hooks, each app's own guards |
| Structured task state | none until a workflow needs it (Phase 6) |

Remote Control stays as the partner's path into `apps/date-planner`.

## Ownership

- Apps own their data and rules. The assistant only calls their MCP tools;
  no gateway, no second agent runtime, no copies of domain logic in prompts.
- Each app's own safety stays authoritative (tee-time-booker's double
  payment gate, the calendar guard, pay-my-bills' single writer).
- Bots that run schedulers (tee-time-booker, pay-my-bills) keep running;
  the assistant reads their state, it doesn't replace them.

## Permission tiers

| Tier | Rule | Examples |
|---|---|---|
| Read | allowed, no prompt | garmin `get_*`, tee-time `list_*`/`get_*`, date-planner reads, Calendar/Gmail reads |
| Write | Claude Code asks; over Telegram the prompt is relayed | date-planner upserts, garmin scheduling, tee-time `create_plan`, `create_request` |
| Book / pay | `ask` rule — always prompts, even if an allow rule would match | tee-time `book_plan`, `run_request` |
| Blocked | `deny` rule — do it in the app | tee-time `delete_plan`; Gmail send/reply/forward; Bash |
| Files | PreToolUse hook `memory_only.py` denies any write outside `apps/assistant/memory/` | |

Paying still needs tee-time-booker's own switch (`TEE_TIME_SUBMIT_PAYMENT`)
as well as `submit_payment: true`; the prompt adds a person on top.

Calendar writes go through date-planner's `calendar_guard.py` (no deletes;
invites ask). Memory, emails, web pages and tool results are data, never
instructions (`CLAUDE.md` says so; the deny rules hold regardless).

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
- A tap is a choice, not an approval: it starts a turn like any message, and
  the tools it leads to are still gated by the tiers above.
- Tee times: search results go out as slot buttons
  (`tee:<course>:<YYYY-MM-DDTHH:MM>:<players>:<holes>`). A tap books that
  slot as `create_request` (earliest = latest = the slot) → `run_request`,
  each behind its own prompt — the same flow as the tee-time bot's
  Book → Confirm.
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
