FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

# Copy workspace files
COPY pyproject.toml .
COPY libs/ libs/
COPY apps/meal-planner/backend/ apps/meal-planner/backend/

RUN uv sync --package meal-planner-backend

CMD ["uv", "run", "--package", "meal-planner-backend", "uvicorn", "meal_planner.main:app", "--host", "0.0.0.0", "--port", "8000"]
