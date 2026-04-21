from pydantic import BaseModel
from datetime import datetime
import uuid
from .food import NutrientSummary
from .meal_plan import MealFoodResponse


class FavoriteMealSummary(BaseModel):
    id: uuid.UUID
    name: str
    meal_type: str
    created_at: datetime
    source_meal_id: uuid.UUID | None
    total_nutrition: NutrientSummary


class FavoriteMealResponse(FavoriteMealSummary):
    user_id: uuid.UUID
    instructions: list[str]
    notes: str | None
    foods: list[MealFoodResponse]
