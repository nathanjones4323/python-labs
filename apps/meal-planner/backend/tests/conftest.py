"""
Shared fixtures for meal-planner backend tests.

All tests run against an in-memory SQLite-like mock via unittest.mock.
The real DB and Anthropic API are never called.
"""

import uuid
import json
import pytest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Minimal nutrition dict used throughout tests
# ---------------------------------------------------------------------------
ZERO_NUTRIENTS = {
    "calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0,
    "fiber_g": 0.0, "sugar_g": 0.0, "saturated_fat_g": 0.0,
    "cholesterol_mg": 0.0, "sodium_mg": 0.0, "calcium_mg": 0.0,
    "iron_mg": 0.0, "magnesium_mg": 0.0, "phosphorus_mg": 0.0,
    "potassium_mg": 0.0, "zinc_mg": 0.0, "vitamin_a_ug": 0.0,
    "vitamin_c_mg": 0.0, "vitamin_d_ug": 0.0, "vitamin_e_mg": 0.0,
    "vitamin_k_ug": 0.0, "vitamin_b6_mg": 0.0, "vitamin_b12_ug": 0.0,
    "folate_ug": 0.0, "thiamin_mg": 0.0, "riboflavin_mg": 0.0,
    "niacin_mg": 0.0,
}

CHICKEN_NUTRIENTS = {**ZERO_NUTRIENTS, "calories": 165.0, "protein_g": 31.0, "fat_g": 3.6, "carbs_g": 0.0}
SALMON_NUTRIENTS  = {**ZERO_NUTRIENTS, "calories": 208.0, "protein_g": 20.0, "fat_g": 13.0, "carbs_g": 0.0}
RICE_NUTRIENTS    = {**ZERO_NUTRIENTS, "calories": 130.0, "protein_g": 2.7, "fat_g": 0.3, "carbs_g": 28.0}


# ---------------------------------------------------------------------------
# Shared test IDs
# ---------------------------------------------------------------------------
USER_ID   = str(uuid.uuid4())
PLAN_ID   = str(uuid.uuid4())
MEAL_ID   = str(uuid.uuid4())
FOOD_FDC_CHICKEN = 331960
FOOD_FDC_SALMON  = 175168
FOOD_FDC_RICE    = 169756


def _make_meal_row():
    return (
        MEAL_ID, "meal_testexternal001", "Grilled Chicken Bowl", "dinner", "2026-04-22",
        ["Grill chicken 6 min per side", "Serve over rice"],
        None,
    )


def _make_food_rows():
    return [
        (str(uuid.uuid4()), "mfood_testexternal01", FOOD_FDC_CHICKEN, "Chicken breast, grilled", CHICKEN_NUTRIENTS, 150.0, None),
        (str(uuid.uuid4()), "mfood_testexternal02", FOOD_FDC_RICE,    "Rice, white, cooked",     RICE_NUTRIENTS,    200.0, None),
    ]


def _make_fav_id():
    return str(uuid.uuid4())
