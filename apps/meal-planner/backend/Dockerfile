# Build context is the repository root (uv workspace lives there).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

# ── Dependency layer ─────────────────────────────────────────────────────────
# Copy only the manifests first so editing source doesn't reinstall the world.
# --no-install-workspace resolves third-party deps but skips the local packages,
# which are added in the next layer.
COPY pyproject.toml uv.lock ./
COPY libs/db/pyproject.toml libs/db/
COPY apps/meal-planner/backend/pyproject.toml apps/meal-planner/backend/
RUN uv sync --frozen --package meal-planner-backend --no-install-workspace

# ── Source layer ─────────────────────────────────────────────────────────────
COPY libs/ libs/
COPY apps/meal-planner/backend/ apps/meal-planner/backend/
RUN uv sync --frozen --package meal-planner-backend

EXPOSE 8000

CMD ["uvicorn", "meal_planner.main:app", "--host", "0.0.0.0", "--port", "8000"]
