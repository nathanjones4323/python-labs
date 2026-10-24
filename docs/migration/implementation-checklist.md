# Implementation checklist

## Milestone 1: discovery and spike (this branch)
- [x] Current state, target state, compatibility matrix
- [x] `apps/assistant`: `.mcp.json` (date-planner, garmin, tee-time-booker),
      `.claude/settings.json` (tiers), `memory_only.py` hook, `CLAUDE.md`,
      `memory/USER.md`
- [x] Hook and settings tests: `uvx pytest apps/assistant/tests`
- [x] Headless check: reads work (garmin), memory recall works, booking,
      plan deletion and writing outside `memory/` are reported as not done
- [ ] Telegram, done by you:
  1. `brew install bun`
  2. BotFather `/newbot` → a **new** token
  3. `claude` in `apps/assistant` → `/plugin install telegram@claude-plugins-official`
     (user scope) → `/telegram:configure <token>`
  4. `claude --channels plugin:telegram@claude-plugins-official`
  5. Message the bot → `/telegram:access pair <code>` →
     `/telegram:access policy allowlist`
- [ ] Phone acceptance: a garmin read with no prompt; memory recall; a
      date-planner write sends a permission prompt to Telegram, and a
      denial is reported as not done; a booking request is refused

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
Remove `apps/assistant` (and the LaunchAgent once installed). No app
changed; each still runs exactly as before.
