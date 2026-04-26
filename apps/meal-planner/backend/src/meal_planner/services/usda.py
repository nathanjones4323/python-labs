"""
USDA FoodData Central client.

Uses Foundation Foods and SR Legacy data types only — these cover whole foods
with rigorous nutrient analysis. Branded/commercial products are excluded.

Free API key signup: https://fdc.nal.usda.gov/api-key-signup.html
DEMO_KEY works for low-volume development (30 req/hr, 50 req/day).

Key nutrient IDs (per 100g basis):
  1008 = Energy (kcal)
  1003 = Protein
  1004 = Total Fat
  1005 = Carbohydrate
  1079 = Fiber
  2000 = Total Sugars
  1258 = Saturated Fat
  1253 = Cholesterol
  1093 = Sodium
  1087 = Calcium
  1089 = Iron
  1090 = Magnesium
  1091 = Phosphorus
  1092 = Potassium
  1095 = Zinc
  1106 = Vitamin A (RAE)
  1162 = Vitamin C
  1114 = Vitamin D
  1109 = Vitamin E
  1185 = Vitamin K
  1175 = Vitamin B6
  1178 = Vitamin B12
  1177 = Folate
  1165 = Thiamin
  1166 = Riboflavin
  1167 = Niacin
"""

import httpx
from meal_planner.config import USDA_API_KEY, USDA_BASE_URL, USDA_DATA_TYPES
from meal_planner.models.food import FoodResult, NutrientSummary

NUTRIENT_MAP = {
    1008: "calories",
    # 1062 intentionally omitted: SR Legacy stores it in kJ, not kcal — including
    # it causes ~4x calorie inflation for oils and fats.
    2048: "calories",   # Energy (Atwater Specific Factors, kcal) — Foundation foods
    1003: "protein_g",
    1004: "fat_g",
    1005: "carbs_g",
    1079: "fiber_g",
    2000: "sugar_g",
    1258: "saturated_fat_g",
    1253: "cholesterol_mg",
    1093: "sodium_mg",
    1087: "calcium_mg",
    1089: "iron_mg",
    1090: "magnesium_mg",
    1091: "phosphorus_mg",
    1092: "potassium_mg",
    1095: "zinc_mg",
    1106: "vitamin_a_ug",
    1162: "vitamin_c_mg",
    1114: "vitamin_d_ug",
    1109: "vitamin_e_mg",
    1185: "vitamin_k_ug",
    1175: "vitamin_b6_mg",
    1178: "vitamin_b12_ug",
    1177: "folate_ug",
    1165: "thiamin_mg",
    1166: "riboflavin_mg",
    1167: "niacin_mg",
}


def _parse_nutrients(food_nutrients: list[dict]) -> NutrientSummary:
    data: dict = {}
    for n in food_nutrients:
        nutrient = n.get("nutrient", n)
        nid = nutrient.get("id") or nutrient.get("nutrientId")
        # Prefer "amount" (detail API) over "value" (search API); use explicit None
        # check so a genuine 0.0 amount is not skipped by Python's truthiness rules.
        raw = n.get("amount")
        if raw is None:
            raw = n.get("value")
        amount = raw if raw is not None else 0.0
        if nid in NUTRIENT_MAP:
            field = NUTRIENT_MAP[nid]
            val = round(float(amount), 4)
            # Two IDs map to calories: 1008 (kcal) and 2048 (kcal, Atwater
            # Specific). ID 1062 is excluded from NUTRIENT_MAP because SR Legacy
            # stores it in kJ, causing ~4x inflation. Both remaining IDs are kcal
            # so taking the max is safe and order-independent.
            if field == "calories":
                data["calories"] = max(data.get("calories", 0.0), val)
            else:
                data[field] = val
    result = NutrientSummary(**data)
    # Fallback: calculate calories from macros if missing or zero
    if result.calories == 0 and (result.protein_g or result.carbs_g or result.fat_g):
        result = result.model_copy(update={
            "calories": round(result.protein_g * 4 + result.carbs_g * 4 + result.fat_g * 9, 4)
        })
    return result


def _to_food_result(item: dict) -> FoodResult:
    nutrients = _parse_nutrients(item.get("foodNutrients", []))
    raw_category = item.get("foodCategory") or item.get("foodCategoryLabel")
    if isinstance(raw_category, dict):
        raw_category = raw_category.get("description")
    return FoodResult(
        fdc_id=item["fdcId"],
        description=item["description"],
        food_category=raw_category,
        data_type=item.get("dataType"),
        nutrients_per_100g=nutrients,
    )


async def search_foods(query: str, limit: int = 10) -> list[FoodResult]:
    """Search USDA FDC for whole food ingredients."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{USDA_BASE_URL}/foods/search",
            params={"api_key": USDA_API_KEY},
            json={
                "query": query,
                "dataType": USDA_DATA_TYPES,
                "pageSize": limit,
                "sortBy": "dataType.keyword",
                "sortOrder": "asc",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        foods = data.get("foods", [])
        return [_to_food_result(f) for f in foods]


async def get_food(fdc_id: int) -> FoodResult | None:
    """Fetch full nutrient detail for a single food by FDC ID."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{USDA_BASE_URL}/food/{fdc_id}",
            params={"api_key": USDA_API_KEY},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _to_food_result(resp.json())


def scale_nutrients(nutrients: NutrientSummary, quantity_g: float) -> NutrientSummary:
    """Scale per-100g nutrients to the given quantity."""
    factor = quantity_g / 100.0
    return NutrientSummary(
        **{k: round(v * factor, 4) for k, v in nutrients.model_dump().items()}
    )
