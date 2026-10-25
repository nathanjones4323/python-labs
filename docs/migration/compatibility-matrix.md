# MCP compatibility matrix

Status of each server under `apps/assistant` (checked 2026-10-23 with
`claude mcp list` and a headless `claude -p` run from a worktree).

| Server | Launch (from `apps/assistant`) | Transport | Needs | Worktree vs main checkout | Side-effecting tools → tier | Status |
|---|---|---|---|---|---|---|
| date-planner | `uv run --directory ../date-planner date-planner-mcp` | stdio | Postgres; `.env` / `.env.local` read from `apps/date-planner` | worktree → dev 5434; main → prod 5435 | DB upserts, `create_date`, stops, ratings → **ask**; `render_date_preview` → ask | connected |
| garmin (Taxuspt fork) | `uvx --python 3.12 --from git+…/garmin_mcp@local/combined garmin-mcp` | stdio | `~/.config/garmin-mcp/tokens` | same either way | workout create/schedule/reschedule/update/push → **ask** | connected; read verified |
| tee-time-booker | `uv run --directory ../tee-time-booker --package tee-time-booker --extra mcp python -m tee_time_booker.mcp` | stdio | `apps/tee-time-booker/.env` (DB, logins, card) | **worktree has no `.env`** → falls back to localhost:5432 and fails; main checkout works | `create_plan`, `set_plan_auto_book`, `create_request`, `cancel_request` → **ask**; `book_plan`, `run_request`, `delete_plan` → **deny** | connected; reads fail in worktree |
| claude.ai Google Calendar | connector | HTTP | claude.ai account | — | create/update → ask + `calendar_guard.py`; delete → denied by guard | connected |
| claude.ai Gmail | connector | HTTP | claude.ai account | — | send/reply/forward → **deny** | connected |
| pay-my-bills | — (no MCP yet) | — | SQLite, read-only | — | none planned (read-only) | milestone 2 |
| meal-planner | — | — | — | — | — | out of scope |

Notes
- `uv run` syncs inexactly, so launching a server doesn't prune the shared
  `.venv` the pay-my-bills LaunchAgent uses. Avoid `uv sync --package …`
  in the main checkout.
- Date-planner previews (`DATE_PLANNER_PREVIEW_DIR`, `Artifact`) aren't
  wired into the assistant yet; the partner's Remote Control path still has
  them.
