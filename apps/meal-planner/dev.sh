#!/usr/bin/env bash
# Start the full meal-planner stack for local development.
# Run from the repository root: bash apps/meal-planner/dev.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Load .env so DATABASE_URL (and others) are set for every subprocess below.
# set -a exports every assignment; source overrides any existing shell vars.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_ROOT/.env"
  set +a
else
  echo "WARNING: no .env found at $REPO_ROOT/.env — copy .env.example and fill in secrets"
fi

# ── 1. Start DB ──────────────────────────────────────────────────────────────
echo "==> Starting database..."
# --wait blocks until the Docker healthcheck passes, not just pg_isready via
# Unix socket. On a fresh PostGIS volume the container briefly restarts
# postgres during extension init, so TCP connections from the host fail with
# "server closed the connection unexpectedly" even after pg_isready returns OK.
# The healthcheck has start_period + retries so it won't flip healthy until
# the DB is genuinely stable from the outside.
docker compose up -d db --wait

# ── 2. Run migrations ────────────────────────────────────────────────────────
echo "==> Running migrations..."
(cd libs/db && uv run --package db alembic -c alembic.ini upgrade head)

# ── 3. Start backend ─────────────────────────────────────────────────────────
BACKEND_PID=""
trap '[[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null || true' EXIT

echo "==> Starting backend on http://localhost:8000 ..."
uv run --package meal-planner-backend uvicorn meal_planner.main:app \
  --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# ── 4. Start frontend ────────────────────────────────────────────────────────
echo "==> Starting frontend..."
cd apps/meal-planner/frontend
npm install --silent
npm start
