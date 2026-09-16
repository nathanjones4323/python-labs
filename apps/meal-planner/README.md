# Meal Planner

Expo (web) frontend and FastAPI backend for meal planning, with PostgreSQL/PostGIS.

## Prerequisites

- **Docker** (Docker Desktop on macOS) — this is all you need for the Docker path below.

For the non-Docker path you also need:

- **Node.js 20** (see `frontend/package.json` Volta pin)
- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** (workspace installs dependencies from the repo root)

## Quick start (Docker)

Everything — database, migrations, API, and web app — runs with one command from the **repository root**:

```bash
make up
```

That's it. `make up` creates a `.env` from `.env.example` if you don't have one, builds the images, and starts the stack in dependency order:

1. **db** — PostGIS 17, waits until its healthcheck passes
2. **migrate** — runs `alembic upgrade head`, then exits (a one-shot service)
3. **backend** — FastAPI on [http://localhost:8000](http://localhost:8000), starts only after migrations succeed
4. **frontend** — Expo web dev server on [http://localhost:8081](http://localhost:8081)

Open [http://localhost:8081](http://localhost:8081) and you're running. Press **Ctrl-C** to stop.

**Set `ANTHROPIC_API_KEY` in the repo-root `.env`** before using any AI feature — the stack starts without it, but chat and plan generation will fail.

### Seed the food database (once, on a fresh database)

Migrations create the `foods` table but leave it **empty**, and every AI feature
matches ingredients against it. Until it is seeded, recipe and meal-plan
requests come back as a puzzling `422 "Claude did not produce a recipe"` — the
agent searched for every ingredient and found nothing. The backend logs a
warning at startup when the table is empty.

```bash
make seed
```

This runs the USDA FoodData Central ingest as a one-shot container. It takes
**~30 minutes** with a registered `USDA_API_KEY`, and re-running it is a no-op
once the table is populated, so it is safe in a loop or a setup script. It is
deliberately **not** part of `make up`: it is slow and consumes API quota, and
most `up` runs are against an already-seeded volume.

- `make seed` — seed if empty, skip if already populated
- `make reseed` — force a full re-pull (USDA ships quarterly updates)

Get a free key at <https://fdc.nal.usda.gov/api-key-signup.html> and put it in
`.env` as `USDA_API_KEY`. Without one the ingest falls back to `DEMO_KEY`,
which is rate-limited to 30 requests/hour and takes ~14 hours.

If you prefer plain Compose, `docker compose up --build` does the same thing.

### Common commands

| Command | What it does |
| --- | --- |
| `make up` | Start the whole stack (build + run) |
| `make down` | Stop everything, keep the database |
| `make clean` | Stop everything and delete the database volume |
| `make reset` | `clean` then `up` — fresh database from scratch |
| `make migrate` | Run pending migrations against the running database |
| `make seed` | Fill the `foods` table from USDA (skipped if already seeded) |
| `make reseed` | Force a full USDA re-pull |
| `make test` | Run the backend test suite in Docker |
| `make logs` | Tail logs from every service |
| `make shell` | Bash shell in the backend container |
| `make psql` | `psql` session against the dev database |
| `make build` | Rebuild images without starting anything |

Run `make help` to list them from the terminal.

### Hot reload

Both services pick up your edits without a rebuild:

- **Backend** — `libs/` and `backend/src/` are bind-mounted, and the workspace packages are installed editable, so `uvicorn --reload` restarts on save.
- **Frontend** — the whole `frontend/` directory is bind-mounted and Metro rebundles on save. `node_modules` lives in a container-owned volume, so the container's install (Linux native binaries) isn't shadowed by your host's.

You only need to rebuild when dependencies change:

```bash
make build
```

### Ports

Every port is overridable in `.env`: `BACKEND_PORT` (8000), `FRONTEND_PORT` (8081), `POSTGRES_PORT` (5432). Note that the backend's CORS allowlist (`backend/src/meal_planner/main.py`) only covers the standard dev origins, so if you change `FRONTEND_PORT` you'll need to add the new origin there too.

---

## Quick start (no Docker, except the database)

If you'd rather run Python and Node on your host, `dev.sh` starts the DB in Docker and everything else natively:

```bash
cp .env.example .env   # then set ANTHROPIC_API_KEY
bash apps/meal-planner/dev.sh
```

This starts the PostGIS container, waits for it to be healthy, runs migrations, launches the FastAPI backend on port 8000, and starts the Expo web dev server. Press **Ctrl-C** to stop the frontend; the backend is cleaned up automatically.

---

## Manual setup (step by step)

Follow these steps if you want more control over each piece.

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

## Troubleshooting

- **CORS errors in the browser:** the API allows specific dev origins (see `backend/src/meal_planner/main.py`). If your Expo web dev server uses another origin, add it to `CORSMiddleware` or align the dev server port.
- **`make up` fails on the port being in use:** something else (often a previous `dev.sh` run) holds 5432, 8000, or 8081. Stop it, or set `POSTGRES_PORT` / `BACKEND_PORT` / `FRONTEND_PORT` in `.env`.
- **Backend edits aren't picked up:** only `libs/` and `backend/src/` are mounted. Changes to `pyproject.toml` or anything outside those paths need `make build`.
- **Frontend edits aren't picked up:** Metro polls for changes across the bind mount, which can lag on large trees. `docker compose restart frontend` forces a fresh watch.
- **Migrations fail on start:** read them with `docker compose logs migrate`. The backend won't start until `migrate` exits 0, so a bad migration shows up as a backend that never comes up.
- **Stale database:** `make clean` deletes the volume; `make reset` deletes it and rebuilds from scratch. Both wipe the seeded `foods` table, so follow either with `make seed`.
- **Recipes and meal plans fail with 422 "Claude did not produce a recipe":** the `foods` table is empty — run `make seed`. The agent can only build a recipe out of ingredients it can find locally.
- **The USDA ingest fails with a 404:** FoodData Central intermittently serves an HTML 404 page for valid requests. Both the ingest script and the runtime client retry these automatically; if a run still aborts, FDC is having a bad stretch — re-run `make seed` later.
