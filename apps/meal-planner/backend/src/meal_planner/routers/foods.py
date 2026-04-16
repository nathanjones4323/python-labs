from fastapi import APIRouter, Query
from meal_planner.services import usda as usda_svc
from meal_planner.models.food import FoodResult, FoodSearchResponse

router = APIRouter(prefix="/foods", tags=["foods"])


@router.get("/search", response_model=FoodSearchResponse)
async def search_foods(
    q: str = Query(..., description="Ingredient name"),
    limit: int = Query(10, ge=1, le=25),
):
    results = await usda_svc.search_foods(q, limit)
    return FoodSearchResponse(results=results, total=len(results))


@router.get("/{fdc_id}", response_model=FoodResult)
async def get_food(fdc_id: int):
    from fastapi import HTTPException
    food = await usda_svc.get_food(fdc_id)
    if not food:
        raise HTTPException(status_code=404, detail="Food not found")
    return food
