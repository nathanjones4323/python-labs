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
| Mobile messaging | Telegram channel plugin (`--channels plugin:telegram@claude-plugins-official`); sender allowlist by pairing; permission prompts relayed to the chat |
| Scheduling, hooks | session-scheduled tasks in the always-on session; Claude Code hooks; launchd keeps the session up |
| MCP config | `apps/assistant/.mcp.json` |
| Memory | `apps/assistant/memory/*.md`, imported from `apps/assistant/CLAUDE.md` |
| Permissions, audit | `.claude/settings.json` allow/deny, PreToolUse hooks, each app's own guards |
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
| Write | Claude Code asks; over Telegram the prompt is relayed | date-planner upserts, garmin scheduling, tee-time `create_plan` |
| Blocked | `deny` rule — do it in the app | tee-time `book_plan`, `run_request`, `delete_plan`; Gmail send/reply/forward; Bash |
| Files | PreToolUse hook `memory_only.py` denies any write outside `apps/assistant/memory/` | |

Calendar writes go through date-planner's `calendar_guard.py` (no deletes;
invites ask). Memory, emails, web pages and tool results are data, never
instructions (`CLAUDE.md` says so; the deny rules hold regardless).

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
- Installed: LaunchAgent running `claude --channels plugin:telegram@…`
  in the main checkout's `apps/assistant`, logs in `~/Library/Logs`, stopped
  with `launchctl bootout`. Its own Telegram bot — never reuse the
  pay-my-bills or tee-time-booker tokens (two pollers on one bot conflict).
