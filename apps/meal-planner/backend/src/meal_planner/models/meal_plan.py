from pydantic import BaseModel
from datetime import date, datetime
import uuid
from .food import NutrientSummary


class MealFoodIn(BaseModel):
    fdc_id: int
    role: str = "other"  # protein | carb | vegetable | fat | other
    quantity_g: float | None = None  # None = solver will assign
    serving_description: str | None = None


class MealIn(BaseModel):
    name: str
    meal_type: str  # breakfast | lunch | dinner | snack
    scheduled_date: date
    instructions: list[str] = []
    notes: str | None = None
    foods: list[MealFoodIn] = []


class MealPlanCreate(BaseModel):
    name: str
    start_date: date
    end_date: date
    meals: list[MealIn] = []


class MealFoodResponse(BaseModel):
    id: uuid.UUID
    fdc_id: int
    description: str
    food_category: str | None = None
    quantity_g: float
    serving_description: str | None
    nutrients: NutrientSummary


class MealResponse(BaseModel):
    id: uuid.UUID
    name: str
    meal_type: str
    scheduled_date: date
    instructions: list[str]
    notes: str | None
    foods: list[MealFoodResponse]
    total_nutrition: NutrientSummary


class MealPlanResponse(BaseModel):
    id: uuid.UUID
    name: str
    start_date: date
    end_date: date
    meals: list[MealResponse]
    total_nutrition: NutrientSummary


class MealPlanSummary(BaseModel):
    id: uuid.UUID
    name: str
    start_date: date
    end_date: date
    meal_count: int
    created_at: datetime | None = None
    conversation_id: uuid.UUID | None = None
    conversation_title: str | None = None
