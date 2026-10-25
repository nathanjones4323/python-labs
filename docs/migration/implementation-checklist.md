# Implementation checklist

## Milestone 1: discovery and spike (this branch)
- [x] Current state, target state, compatibility matrix
- [x] `apps/assistant`: `.mcp.json` (date-planner, garmin, tee-time-booker),
      `.claude/settings.json` (tiers), `memory_only.py` hook, `CLAUDE.md`,
      `memory/USER.md`
- [x] Hook and settings tests: `uvx pytest apps/assistant/tests`
- [x] Headless check: reads work (garmin), memory recall works, booking,
      plan deletion and writing outside `memory/` are reported as not done
- [x] Fork the Telegram plugin (v0.0.7) into `plugins/telegram` with
      inline buttons (`buttons` on `reply`/`edit_message`, `btn:*` taps →
      channel event with `meta.button`); local marketplace
      `.claude-plugin/marketplace.json` (`python-labs`)
- [x] Fork checked against a fake Bot API over MCP stdio: keyboard shape,
      data limits, tap → event + edit, stranger refused, double tap
      ignored, `perm:*` unchanged
- [x] Tee-time booking moves from deny to ask (`book_plan`, `run_request`
      in `permissions.ask`); `delete_plan` stays denied. `CLAUDE.md` sends
      search results as slot buttons and books a tapped slot
- [ ] Telegram, done by you:
  1. `brew install bun`
  2. BotFather `/newbot` → a **new** token
  3. `claude` in `apps/assistant` →
     `/plugin marketplace add <this checkout>` (the worktree until merged;
     after merging, remove it and add the main checkout) →
     `/plugin install telegram@python-labs` (user scope; uninstall
     `telegram@claude-plugins-official` if it's there) →
     `/telegram:configure <token>`
  4. `claude --dangerously-load-development-channels plugin:telegram@python-labs`
  5. Message the bot → `/telegram:access pair <code>` →
     `/telegram:access policy allowlist`
- [ ] Phone acceptance: a garmin read with no prompt; memory recall; a
      date-planner write sends a permission prompt to Telegram, and a
      denial is reported as not done; plan deletion is refused
- [ ] Phone acceptance, buttons: a tee-time search comes back as slot
      buttons; a tap edits the card to show the slot and leads to a
      `create_request` prompt then a `run_request` prompt; denying either
      is reported as not done; a dry run (payment switch off) is reported
      as nothing booked

## Milestone 2: install and widen
- [ ] Merge; install from the main checkout (tee-time-booker and prod
      date-planner only work there)
- [ ] LaunchAgent `com.nathanjones.assistant` (RunAtLoad, KeepAlive, log
      path), kill switch `launchctl bootout gui/$(id -u)/com.nathanjones.assistant`
- [ ] Read-only pay-my-bills MCP (`query_only` SQLite) + allowlist
- [ ] Memory: PROJECTS, ROUTINES, DECISIONS, `journal/`; a weekly
      consolidation routine
- [ ] Date-planner previews in the assistant (`DATE_PLANNER_PREVIEW_DIR`,
      `Artifact`, `ArtifactData`)

## Milestone 3: automation and audit
- [ ] Scheduled tasks in the always-on session (morning brief, bill
      reminders), one at a time
- [ ] PostToolUse audit hook: JSONL of tool name, decision, outcome, no payloads
- [ ] Clearer results from side-effecting tools where they're vague
- [ ] Phase 6 SQLite only if a workflow needs resumable state

## Rollback
Remove `apps/assistant`, `plugins/telegram` and `.claude-plugin/` (and the
LaunchAgent once installed); reinstall `telegram@claude-plugins-official`
if wanted (same state dir, same pairing). No app changed; each still runs
exactly as before.
