# Current state (2026-10-23)

What the monorepo runs today, before the assistant. Env vars are listed by
name only; values live in the files named under "Secrets".

## Shape

uv workspace (`pyproject.toml`): `libs/db`, `apps/meal-planner/backend`,
`apps/tee-time-booker`, `apps/date-planner`, `apps/garmin-mcp`,
`apps/pay-my-bills`. The root `Makefile` and `docker-compose.yml` only cover
meal-planner. No root `CLAUDE.md`; Claude Code config lives per app in
`apps/*/.claude/` and `apps/*/.mcp.json`.

## Apps

### date-planner
- **MCP:** `date-planner-mcp` (fastmcp). stdio from `apps/date-planner/.mcp.json`;
  `--http` serves 127.0.0.1:8002 and refuses to start without
  `DATE_PLANNER_HTTP_TOKEN`.
- **Writes (DB only):** `upsert_couple_profile`, `upsert_member`,
  `edit_member_preferences`, `upsert_venue`, `create_date`, `update_date`,
  `add_stop`, `update_stop`, `reorder_stops`, `remove_stop`, `rate_date`,
  `clear_rating`; `render_date_preview` writes a file. Everything else is
  read or compute. It never books and never touches Google.
- **Google:** through the claude.ai Calendar/Gmail connectors, guarded by
  `.claude/hooks/calendar_guard.py` (no deletes; invites ask) and `deny.py`
  (no sending email).
- **State:** Postgres 17, SQLAlchemy async + Alembic. Dev 5434 (worktrees,
  tests); prod 5435 (main checkout only, via its `.env.local`).
- **Mobile:** the partner uses it over Remote Control from the main checkout.
- **Tests:** `uv run --package date-planner pytest` (dev DB 5434).

### garmin
- **MCP in use:** the Taxuspt `garmin_mcp` fork (`uvx … @local/combined`),
  configured in `apps/garmin-mcp/.mcp.json`; 17 read tools allowlisted in
  `apps/garmin-mcp/.claude/settings.json`. Writes (create/schedule/
  reschedule/update/push workouts) go through Claude Code's permission
  prompt only — the fork has no previews.
- **Old local server** (`apps/garmin-mcp`, kept until the upstream PRs
  merge): validate → preview → approval token → execute once → verify →
  `operations.jsonl`, dry-run by default.
- **State:** none; tokens in `~/.config/garmin-mcp/tokens`.
- **Tests:** `uv run --package garmin-mcp pytest` (no network).

### tee-time-booker
- **MCP:** `python -m tee_time_booker.mcp` (`mcp` extra), generated from the
  FastAPI routes; stdio or `--http` on 127.0.0.1:8001 (no auth).
- **Read:** `list_courses`, `get_course`, `list_plans`, `get_plan`,
  `search_tee_times`, `find_tee_times` (real browser sign-in),
  `list_requests`, `get_request`, `list_request_runs`, `list_runs`, `get_run`.
- **Write:** `create_plan`, `set_plan_auto_book`, `delete_plan`,
  `create_request`, `cancel_request`; **`book_plan` and `run_request` book
  and can pay** — only when `TEE_TIME_SUBMIT_PAYMENT` is set *and* the call
  passes `submit_payment: true`.
- **Always-on:** its own Telegram bot (`python -m tee_time_booker.bot`) runs
  the scheduler; auto-booking only happens while it runs. The bot has its
  own agent (`bot/agent.py`, anthropic SDK / OpenAI-compatible / Ollama) over
  the same tools.
- **State:** Postgres 5433 in dev, SQLite for self-hosted copies; ORM +
  Alembic. Reads `.env` via `load_dotenv()` — only the main checkout has one.
- **Tests:** `uv run --package tee-time-booker pytest`.

### pay-my-bills
- **No MCP.** Telegram bot polling Gmail (read-only OAuth) every 2 min,
  running as LaunchAgent `com.nathanjones.pay-my-bills` from the main
  checkout. It is the database's only writer.
- **State:** SQLite `~/.local/share/pay-my-bills/bills.db`, ORM + Alembic.
- **Tests:** `uv run --package pay-my-bills pytest apps/pay-my-bills`.

### meal-planner
- FastAPI + Expo, PostGIS 5432 via `libs/db` (raw SQL), anthropic SDK in
  `services/`. No MCP. `make up` / `make test`.

## Secrets (locations, never values)

| Where | Holds |
|---|---|
| root `.env` | meal-planner DB, `ANTHROPIC_API_KEY`, `USDA_API_KEY`, Google client IDs |
| `apps/date-planner/.env` (+ `.env.local` in the main checkout) | `DATABASE_URL`, `GOOGLE_PLACES_API_KEY`, `ORS_API_KEY`, `CARTO_API_KEY`, `DATE_PLANNER_HTTP_TOKEN` |
| `apps/tee-time-booker/.env` (main checkout, mode 600) | course logins, **card details**, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY` |
| `apps/pay-my-bills/.env` (main checkout) | `TELEGRAM_BOT_TOKEN`, roommate payment handles |
| `~/.config/pay-my-bills/` | Gmail OAuth client + token |
| `~/.config/garmin-mcp/tokens` | Garmin session tokens |

## Existing automation of real-world actions
- tee-time-booker: books and pays (gated twice), on a schedule from its bot.
- pay-my-bills: prepares Venmo requests; the user taps to send.
- date-planner skill: tentative Google Calendar holds (guarded by a hook).
- garmin fork: schedules and pushes workouts to the watch (permission prompt).

## Overlaps with an assistant runtime
- Two Telegram bots each with their own loop; tee-time-booker also has its
  own LLM agent. Both stay: the bots own their schedulers, and
  tee-time-booker is self-hosted per person.
- Remote Control for date-planner already gives phone access to one app.
