"""
Deterministic macro solver.

Takes a day's meals with foods that have nutrients_per_100g and quantity_g,
and scales portions iteratively until calorie totals hit the daily target.
"""

from meal_planner.services.usda import scale_nutrients

MEAL_CALORIE_SPLIT: dict[str, float] = {
    "breakfast": 0.25,
    "lunch":     0.35,
    "dinner":    0.35,
    "snack":     0.05,
}


def derive_macro_targets(calorie_target: float, goals: str | None) -> dict:
    """Derive protein/carb/fat gram targets from calorie target and goals string."""
    g = (goals or "").lower()
    if any(k in g for k in ("muscle", "bulk", "strength")):
        pct = {"protein": 0.35, "carbs": 0.40, "fat": 0.25}
    elif any(k in g for k in ("weight loss", "cut", "deficit")):
        pct = {"protein": 0.35, "carbs": 0.35, "fat": 0.30}
    else:
        pct = {"protein": 0.30, "carbs": 0.40, "fat": 0.30}
    return {
        "calories":  calorie_target,
        "protein_g": (calorie_target * pct["protein"]) / 4,
        "carbs_g":   (calorie_target * pct["carbs"])   / 4,
        "fat_g":     (calorie_target * pct["fat"])     / 9,
    }


def solve_day(
    meals: list[dict],
    daily_targets: dict,
    max_iter: int = 5,
    tolerance: float = 0.05,
) -> list[dict]:
    """
    Scale quantity_g for every food in a day until daily calories hit the target.

    meals: list of dicts with keys "meal_type" and "foods".
           Each food dict must have "quantity_g" and "nutrients_per_100g".
    daily_targets: dict with at minimum key "calories".
    Returns meals with updated quantity_g values.
    """
    if not meals or daily_targets["calories"] <= 0:
        return meals

    # Distribute calorie budget across meals proportional to meal type split
    total_split = sum(MEAL_CALORIE_SPLIT.get(m["meal_type"], 0.10) for m in meals)
    if total_split == 0:
        total_split = len(meals) * 0.10

    meal_cal_targets = [
        (MEAL_CALORIE_SPLIT.get(m["meal_type"], 0.10) / total_split) * daily_targets["calories"]
        for m in meals
    ]

    for _ in range(max_iter):
        for i, meal in enumerate(meals):
            meal_cal = sum(
                scale_nutrients(f["nutrients_per_100g"], f["quantity_g"]).calories
                for f in meal["foods"]
            )
            if meal_cal <= 0:
                continue
            ratio = meal_cal_targets[i] / meal_cal
            for f in meal["foods"]:
                f["quantity_g"] = max(10.0, f["quantity_g"] * ratio)

        total_cal = sum(
            scale_nutrients(f["nutrients_per_100g"], f["quantity_g"]).calories
            for m in meals for f in m["foods"]
        )
        if abs(total_cal - daily_targets["calories"]) / daily_targets["calories"] < tolerance:
            break

    return meals
