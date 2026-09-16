import httpx
from fastapi import APIRouter, HTTPException, Query
from meal_planner.services import usda as usda_svc
from meal_planner.models.food import FoodResult, FoodSearchResponse

router = APIRouter(prefix="/foods", tags=["foods"])


@router.get("/search", response_model=FoodSearchResponse)
async def search_foods(
    q: str = Query(..., description="Ingredient name"),
    limit: int = Query(10, ge=1, le=25),
):
    try:
        results = await usda_svc.search_foods(q, limit)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # Retries are exhausted by this point — report it as an upstream
        # failure instead of an unhandled 500 traceback.
        raise HTTPException(
            status_code=502, detail="USDA food database is unavailable. Try again."
        ) from exc
    return FoodSearchResponse(results=results, total=len(results))


@router.get("/{fdc_id}", response_model=FoodResult)
async def get_food(fdc_id: int):
    from fastapi import HTTPException
    food = await usda_svc.get_food(fdc_id)
    if not food:
        raise HTTPException(status_code=404, detail="Food not found")
    return food
