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
- `apps/tee-time-booker` — scheduled Playwright job that books a Soule Park tee time (see its README)
