"""Nutritional aggregation utilities."""

from meal_planner.models.food import NutrientSummary


def sum_nutrients(nutrient_list: list[NutrientSummary]) -> NutrientSummary:
    """Sum a list of NutrientSummary objects into one total."""
    totals: dict = {}
    for nutrients in nutrient_list:
        for field, value in nutrients.model_dump().items():
            totals[field] = round(totals.get(field, 0.0) + (value or 0.0), 4)
    return NutrientSummary(**totals)
