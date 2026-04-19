# Meal Planner

Expo (web) frontend and FastAPI backend for meal planning, with PostgreSQL/PostGIS.

## Prerequisites

- **Node.js 20** (see `frontend/package.json` Volta pin)
- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** (workspace installs dependencies from the repo root)
- **Docker** (recommended) to run PostgreSQL

## 1. Start the database

From the **repository root** (`python-labs`):

```bash
docker compose up -d db
```

This starts PostGIS on port **5432** with user/password/database `postgres` / `postgres` / `app` by default (see `docker-compose.yml`). The backend’s default `DATABASE_URL` matches this.

## 2. Run database migrations

Migrations live under `libs/db`. The Alembic env reads `DATABASE_URL` from the environment only (it does not load `.env` by itself). Either:

**A.** Inline URL (matches the default local Docker DB):

```bash
cd libs/db
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/app" uv run alembic -c alembic.ini upgrade head
cd -   # back to repo root
```

**B.** Reuse the **repository root** `.env` (same `DATABASE_URL` you use for the app):

```bash
cd libs/db
uv run --env-file ../../.env alembic -c alembic.ini upgrade head
cd -   # back to repo root
```

Run this again whenever new migrations are added.

## 3. Run the API

The backend loads a **`.env`** file via `python-dotenv` (`load_dotenv(find_dotenv())` in `backend/src/meal_planner/config.py`). Put a `.env` at the **repository root** (or any parent of your shell’s current directory—`find_dotenv` walks upward) and run uvicorn from the **repository root** so that file is found. Typical entries:

- `ANTHROPIC_API_KEY` — required for chat / AI features  
- `USDA_API_KEY` — optional (the app falls back to `DEMO_KEY` if unset)  
- `DATABASE_URL` — optional; defaults match the local Docker DB (see `config.py`)

You do **not** need to `export` those variables in the terminal for the API process.

From the **repository root**:

```bash
uv run --package meal-planner-backend uvicorn meal_planner.main:app --reload --host 0.0.0.0 --port 8000
```

- Health check: [http://localhost:8000/health](http://localhost:8000/health)

## 4. Run the web app

From the **repository root**:

```bash
cd apps/meal-planner/frontend
npm install
npm start
```

The UI talks to **http://localhost:8000** by default (`frontend/lib/api.ts`). To point at another host, set `EXPO_PUBLIC_API_URL` before starting (or configure `expo.extra.apiUrl` in `app.json`).

## Optional: run backend with Docker

From the **repository root**, with `ANTHROPIC_API_KEY` set in your environment or a `.env` file loaded by Compose:

```bash
docker compose up --build backend db
```

You still need to run the migration step from your machine (step 2) against `localhost:5432` unless you automate migrations elsewhere.

## Troubleshooting

- **CORS errors in the browser:** the API allows specific dev origins (see `backend/src/meal_planner/main.py`). If your Expo web dev server uses another origin, add it to `CORSMiddleware` or align the dev server port.
