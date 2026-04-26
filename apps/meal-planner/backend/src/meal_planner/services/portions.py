ROLE_DEFAULTS_G: dict[str, float] = {
    "protein":   140.0,
    "carb":      160.0,
    "vegetable": 100.0,
    "fat":        10.0,
    "other":     100.0,
}


def assign_default_portions(foods: list[dict]) -> list[dict]:
    """Assign default gram weights by role when quantity_g is not provided."""
    result = []
    for f in foods:
        qty = f.get("quantity_g")
        if qty is None:
            qty = ROLE_DEFAULTS_G.get(f.get("role", "other"), 100.0)
        result.append({**f, "quantity_g": qty})
    return result
