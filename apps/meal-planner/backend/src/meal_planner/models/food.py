from pydantic import BaseModel


class Nutrient(BaseModel):
    id: int
    name: str
    amount: float
    unit: str


class NutrientSummary(BaseModel):
    calories: float = 0
    protein_g: float = 0
    fat_g: float = 0
    carbs_g: float = 0
    fiber_g: float = 0
    sugar_g: float = 0
    saturated_fat_g: float = 0
    cholesterol_mg: float = 0
    sodium_mg: float = 0
    calcium_mg: float = 0
    iron_mg: float = 0
    magnesium_mg: float = 0
    phosphorus_mg: float = 0
    potassium_mg: float = 0
    zinc_mg: float = 0
    vitamin_a_ug: float = 0
    vitamin_c_mg: float = 0
    vitamin_d_ug: float = 0
    vitamin_e_mg: float = 0
    vitamin_k_ug: float = 0
    vitamin_b6_mg: float = 0
    vitamin_b12_ug: float = 0
    folate_ug: float = 0
    thiamin_mg: float = 0
    riboflavin_mg: float = 0
    niacin_mg: float = 0


class FoodResult(BaseModel):
    fdc_id: int
    description: str
    food_category: str | None = None
    data_type: str | None = None
    nutrients_per_100g: NutrientSummary = NutrientSummary()


class FoodSearchResponse(BaseModel):
    results: list[FoodResult]
    total: int
