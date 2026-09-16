# Local dev entrypoints for the meal-planner stack. See README.md.
.PHONY: help up dev down clean build migrate seed reseed logs shell psql test reset

COMPOSE := docker compose

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

.env:
	@echo "==> No .env found — creating one from .env.example"
	@cp .env.example .env
	@echo "    Edit .env and set ANTHROPIC_API_KEY before using AI features."

up: .env ## Start the whole stack (db, migrations, backend, frontend)
	$(COMPOSE) up --build

dev: up ## Alias for `make up`

down: ## Stop all services (database volume is kept)
	$(COMPOSE) down

clean: ## Stop all services and delete the database volume
	$(COMPOSE) down -v

build: .env ## Rebuild images without starting anything
	$(COMPOSE) build

migrate: .env ## Run pending Alembic migrations against the running database
	$(COMPOSE) run --rm migrate

seed: .env ## Fill the foods table from USDA (skipped if already seeded, ~30 min)
	$(COMPOSE) --profile seed run --rm seed

reseed: .env ## Force a full USDA re-pull even if the foods table is populated
	$(COMPOSE) --profile seed run --rm seed python scripts/ingest_usda.py

logs: ## Tail logs from every service
	$(COMPOSE) logs -f

shell: ## Open a shell in the backend container
	$(COMPOSE) run --rm --entrypoint bash backend

psql: ## Open a psql session against the dev database
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-app}

test: .env ## Run the backend test suite in Docker
	$(COMPOSE) run --rm --entrypoint "uv run --package meal-planner-backend pytest" \
		--workdir /app/apps/meal-planner/backend backend

reset: clean up ## Wipe the database and start fresh
