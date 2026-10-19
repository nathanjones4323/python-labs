# python-labs
Monorepo to hold my personal projects

## Environment Setup

Copy `.env.example` to `.env` at the repo root and fill in the values:

```bash
cp .env.example .env
```

The `.env` file is git-ignored and never committed.

## Apps

- `apps/meal-planner` — Expo frontend + FastAPI backend (see its README)
- `apps/tee-time-booker` — scheduled Playwright job that books a tee time, with one adapter per booking site (foreUP, GolfNow) behind a shared interface (see its README)
- `apps/garmin-mcp` — local MCP server for a personal Garmin Connect account: training data, strength/running workouts, calendar scheduling and pushing to a watch, with previewed, approved and verified writes (see its README)
