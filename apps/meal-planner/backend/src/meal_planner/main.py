import logging
from contextlib import asynccontextmanager

from db import get_conn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meal_planner.config import ANTHROPIC_API_KEY
from meal_planner.routers import users, foods, meal_plans, chat, meals, auth, favorites, recipes

logger = logging.getLogger("uvicorn.error")


def _check_local_dev_setup() -> None:
    """Warn about the two setup gaps that otherwise surface as puzzling errors."""
    if not ANTHROPIC_API_KEY:
        # Without this the Anthropic SDK raises an opaque TypeError deep inside
        # a request instead of at startup, so say it plainly while it's visible.
        logger.warning(
            "ANTHROPIC_API_KEY is not set — every AI feature (chat, recipe "
            "analysis, meal plan generation) will fail with a 500. Set it in "
            ".env and restart the backend."
        )

    # The foods table is filled by `make seed`, not by migrations. An empty one
    # is not a crash — it is worse: the AI features run, match no ingredients,
    # and return a puzzling 422.
    try:
        with get_conn() as conn:
            count = conn.execute("SELECT count(*) FROM foods").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - never block startup on this check
        logger.warning("Could not check whether the foods table is seeded: %s", exc)
        return
    if count == 0:
        logger.warning(
            "The `foods` table is empty — recipe and meal-plan features will "
            "fail with 422 because no ingredient can be matched. "
            "Run `make seed` to load the USDA food database."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_local_dev_setup()
    yield


app = FastAPI(title="Meal Planner API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://localhost:8082", "http://localhost:8083", "http://localhost:19006", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(foods.router)
app.include_router(meal_plans.router)
app.include_router(chat.router)
app.include_router(meals.router)
app.include_router(favorites.router)
app.include_router(recipes.router)


@app.get("/health")
def health():
    return {"status": "ok"}
