"""
LP-based meal portion solver using OR-Tools GLOP.

Takes a day's meals with foods that have nutrients_per_100g and quantity_g
(initial guess), and solves for gram quantities that minimise deviation from
calorie and macro targets while preserving the per-meal calorie split.
"""

from ortools.linear_solver import pywraplp

from meal_planner.models.food import NutrientSummary

MEAL_CALORIE_SPLIT: dict[str, float] = {
    "breakfast": 0.25,
    "lunch":     0.35,
    "dinner":    0.35,
    "snack":     0.05,
}

FOOD_MIN_G = 10.0
FOOD_MAX_G = 500.0

# Objective weights in kcal-equivalent units so terms are comparable
W_CALORIES = 1.0
W_PROTEIN  = 4.0   # 4 kcal/g protein
W_CARBS    = 4.0   # 4 kcal/g carbs
W_FAT      = 9.0   # 9 kcal/g fat
W_SMOOTH   = 0.01  # small penalty for straying from the initial guess


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


def _nut_coeff(food: dict, field: str) -> float:
    """Return the per-gram nutrient coefficient for a food."""
    n100 = food["nutrients_per_100g"]
    if isinstance(n100, NutrientSummary):
        val = getattr(n100, field, 0.0)
    elif isinstance(n100, dict):
        val = n100.get(field, 0.0)
    else:
        val = 0.0
    return float(val or 0.0) / 100.0


def _add_abs_dev_constraints(solver, x_vars, coeffs, dev_var, target, inf):
    """Linearise |dot(coeffs, x_vars) - target| ≤ dev_var with two constraints."""
    # dot - dev ≤ target
    ct = solver.Constraint(-inf, target)
    for xi, c in zip(x_vars, coeffs):
        if c:
            ct.SetCoefficient(xi, c)
    ct.SetCoefficient(dev_var, -1.0)

    # dot + dev ≥ target
    ct = solver.Constraint(target, inf)
    for xi, c in zip(x_vars, coeffs):
        if c:
            ct.SetCoefficient(xi, c)
    ct.SetCoefficient(dev_var, 1.0)


def solve_day(
    meals: list[dict],
    daily_targets: dict,
    max_iter: int = 5,        # unused, kept for API compatibility
    tolerance: float = 0.05,  # unused, kept for API compatibility
) -> list[dict]:
    """
    Solve food gram quantities for a day using OR-Tools GLOP LP.

    meals: list of dicts with keys "meal_type" and "foods".
           Each food dict must have "quantity_g" (initial guess) and
           "nutrients_per_100g" (NutrientSummary or dict).
    daily_targets: dict with at minimum "calories"; optionally "protein_g",
                   "carbs_g", "fat_g".
    Returns meals with quantity_g updated to solver output (rounded to 1g).
    """
    if not meals or daily_targets["calories"] <= 0:
        return meals

    # Flatten meals → indexed list of (meal_idx, food_idx, food_dict)
    food_entries: list[tuple[int, int, dict]] = [
        (mi, fi, food)
        for mi, meal in enumerate(meals)
        for fi, food in enumerate(meal["foods"])
    ]
    if not food_entries:
        return meals

    n = len(food_entries)

    # Per-meal calorie budget from the fixed split
    total_split = sum(MEAL_CALORIE_SPLIT.get(m["meal_type"], 0.10) for m in meals)
    if total_split == 0:
        total_split = len(meals) * 0.10
    meal_cal_targets = [
        (MEAL_CALORIE_SPLIT.get(m["meal_type"], 0.10) / total_split) * daily_targets["calories"]
        for m in meals
    ]

    # meal_idx → flat indices into food_entries
    meal_to_idxs: dict[int, list[int]] = {}
    for flat_i, (mi, _, _) in enumerate(food_entries):
        meal_to_idxs.setdefault(mi, []).append(flat_i)

    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        return meals

    inf = solver.infinity()

    # Clamp initial guesses to variable bounds
    initials = [
        max(FOOD_MIN_G, min(FOOD_MAX_G, float(fe[2]["quantity_g"])))
        for fe in food_entries
    ]

    # Decision variables: grams per food
    x = [solver.NumVar(FOOD_MIN_G, FOOD_MAX_G, f"x{i}") for i in range(n)]

    # Deviation variables (one per nutrient axis)
    dev_cal     = solver.NumVar(0, inf, "dev_cal")
    dev_protein = solver.NumVar(0, inf, "dev_protein") if "protein_g" in daily_targets else None
    dev_carbs   = solver.NumVar(0, inf, "dev_carbs")   if "carbs_g"   in daily_targets else None
    dev_fat     = solver.NumVar(0, inf, "dev_fat")     if "fat_g"     in daily_targets else None

    # Smoothing variables: abs_dev[i] ≥ |x[i] - initial[i]|
    abs_dev = [solver.NumVar(0, inf, f"ad{i}") for i in range(n)]

    # Per-meal calorie deviation constraints (all funnel into dev_cal)
    for mi, target_m in enumerate(meal_cal_targets):
        idxs = meal_to_idxs.get(mi, [])
        if not idxs:
            continue
        cal_coeffs = [_nut_coeff(food_entries[i][2], "calories") for i in idxs]
        _add_abs_dev_constraints(solver, [x[i] for i in idxs], cal_coeffs, dev_cal, target_m, inf)

    # Global calorie deviation
    all_cal_coeffs = [_nut_coeff(fe[2], "calories") for fe in food_entries]
    _add_abs_dev_constraints(solver, x, all_cal_coeffs, dev_cal, daily_targets["calories"], inf)

    # Macro deviation constraints
    for field, key, dev_var in [
        ("protein_g", "protein_g", dev_protein),
        ("carbs_g",   "carbs_g",   dev_carbs),
        ("fat_g",     "fat_g",     dev_fat),
    ]:
        if dev_var is None:
            continue
        macro_coeffs = [_nut_coeff(fe[2], field) for fe in food_entries]
        _add_abs_dev_constraints(solver, x, macro_coeffs, dev_var, daily_targets[key], inf)

    # Smoothing constraints: linearise |x[i] - initial[i]|
    for xi, ad, init in zip(x, abs_dev, initials):
        ct = solver.Constraint(-inf, init)
        ct.SetCoefficient(xi, 1.0)
        ct.SetCoefficient(ad, -1.0)

        ct = solver.Constraint(init, inf)
        ct.SetCoefficient(xi, 1.0)
        ct.SetCoefficient(ad, 1.0)

    # Objective: weighted sum of deviations + smoothing
    obj = solver.Objective()
    obj.SetCoefficient(dev_cal, W_CALORIES)
    if dev_protein is not None:
        obj.SetCoefficient(dev_protein, W_PROTEIN)
    if dev_carbs is not None:
        obj.SetCoefficient(dev_carbs, W_CARBS)
    if dev_fat is not None:
        obj.SetCoefficient(dev_fat, W_FAT)
    for ad in abs_dev:
        obj.SetCoefficient(ad, W_SMOOTH)
    obj.SetMinimization()

    status = solver.Solve()

    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return meals

    # Write solved quantities back (round to nearest 1g)
    for flat_i, (mi, fi, _) in enumerate(food_entries):
        meals[mi]["foods"][fi]["quantity_g"] = round(max(FOOD_MIN_G, x[flat_i].solution_value()))

    return meals
