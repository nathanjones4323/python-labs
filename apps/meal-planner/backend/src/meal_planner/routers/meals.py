import uuid
import json
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import get_conn
from meal_planner.models.meal_plan import MealFoodIn, MealFoodResponse, MealResponse
from meal_planner.models.food import NutrientSummary
from meal_planner.models.user import UserProfileResponse
from meal_planner.services import usda as usda_svc
from meal_planner.services.nutrition import sum_nutrients
from meal_planner.routers.meal_plans import _get_or_cache_food

router = APIRouter(prefix="/meals", tags=["meals"])


class MealUpdateBody(BaseModel):
    name: Optional[str] = None
    meal_type: Optional[str] = None
    instructions: Optional[list[str]] = None
    notes: Optional[str] = None
    foods: Optional[list[MealFoodIn]] = None


class MealAIEditBody(BaseModel):
    instruction: str
    user_id: uuid.UUID


async def _load_meal_response(conn, meal_id: str) -> MealResponse:
    """Read a meal and its foods from DB and return a MealResponse."""
    mr = conn.execute(
        "SELECT id, external_id, name, meal_type, scheduled_date, instructions, notes FROM meals WHERE id = %s",
        (meal_id,),
    ).fetchone()
    if not mr:
        raise HTTPException(status_code=404, detail="Meal not found")

    food_rows = conn.execute(
        """
        SELECT mf.id, mf.external_id, mf.fdc_id, f.description, f.nutrients, mf.quantity_g, mf.serving_description
        FROM meal_foods mf JOIN foods f ON f.fdc_id = mf.fdc_id
        WHERE mf.meal_id = %s
        """,
        (meal_id,),
    ).fetchall()

    food_responses: list[MealFoodResponse] = []
    for fr in food_rows:
        nutrients_per_100g = NutrientSummary(**(fr[4] if fr[4] else {}))
        scaled = usda_svc.scale_nutrients(nutrients_per_100g, float(fr[5]))
        food_responses.append(
            MealFoodResponse(
                id=fr[0],
                external_id=fr[1],
                fdc_id=fr[2],
                description=fr[3],
                quantity_g=float(fr[5]),
                serving_description=fr[6],
                nutrients=scaled,
            )
        )

    return MealResponse(
        id=mr[0],
        external_id=mr[1],
        name=mr[2],
        meal_type=mr[3],
        scheduled_date=mr[4],
        instructions=mr[5] or [],
        notes=mr[6],
        foods=food_responses,
        total_nutrition=sum_nutrients([f.nutrients for f in food_responses]),
    )


@router.put("/{meal_id}", response_model=MealResponse)
async def update_meal(meal_id: uuid.UUID, body: MealUpdateBody):
    with get_conn() as conn:
        # Verify meal exists
        exists = conn.execute(
            "SELECT id FROM meals WHERE id = %s", (str(meal_id),)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Meal not found")

        # Build dynamic UPDATE for meal metadata
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.meal_type is not None:
            updates["meal_type"] = body.meal_type
        if body.instructions is not None:
            updates["instructions"] = body.instructions
        if body.notes is not None:
            updates["notes"] = body.notes

        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            conn.execute(
                f"UPDATE meals SET {set_clause} WHERE id = %s",
                list(updates.values()) + [str(meal_id)],
            )

        # Replace foods if provided
        if body.foods is not None:
            conn.execute("DELETE FROM meal_foods WHERE meal_id = %s", (str(meal_id),))
            for food_in in body.foods:
                food = await _get_or_cache_food(conn, food_in.fdc_id)
                mf_id = uuid.uuid4()
                conn.execute(
                    """
                    INSERT INTO meal_foods (id, meal_id, fdc_id, quantity_g, serving_description)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        str(mf_id),
                        str(meal_id),
                        food_in.fdc_id,
                        food_in.quantity_g,
                        food_in.serving_description,
                    ),
                )

        conn.commit()
        return await _load_meal_response(conn, str(meal_id))


def _load_edit_context(conn, meal_id: str, user_id: str) -> tuple[UserProfileResponse | None, float | None]:
    """Load user profile and other-meals calorie total for the same day."""
    profile_row = conn.execute(
        """
        SELECT user_id, dietary_restrictions, allergies, goals,
               calorie_target, meals_per_day, preferences, onboarding_complete
        FROM user_profiles WHERE user_id = %s
        """,
        (user_id,),
    ).fetchone()
    profile: UserProfileResponse | None = (
        UserProfileResponse(
            user_id=profile_row[0],
            dietary_restrictions=profile_row[1] or [],
            allergies=profile_row[2] or [],
            goals=profile_row[3],
            calorie_target=profile_row[4],
            meals_per_day=profile_row[5] or 3,
            preferences=profile_row[6] or {},
            onboarding_complete=profile_row[7],
        )
        if profile_row else None
    )

    other_kcal_row = conn.execute(
        """
        SELECT COALESCE(SUM((f.nutrients->>'calories')::numeric * mf.quantity_g / 100), 0)
        FROM meals m
        JOIN meal_foods mf ON mf.meal_id = m.id
        JOIN foods f ON f.fdc_id = mf.fdc_id
        WHERE m.meal_plan_id = (SELECT meal_plan_id FROM meals WHERE id = %s)
          AND m.scheduled_date = (SELECT scheduled_date FROM meals WHERE id = %s)
          AND m.id != %s
        """,
        (meal_id, meal_id, meal_id),
    ).fetchone()
    other_meals_kcal: float | None = float(other_kcal_row[0]) if other_kcal_row else None

    return profile, other_meals_kcal


@router.post("/{meal_id}/ai-preview", response_model=MealResponse)
async def ai_preview_meal(meal_id: uuid.UUID, body: MealAIEditBody):
    """Run AI edit and return proposed MealResponse WITHOUT saving to DB."""
    from meal_planner.services.meal_edit import edit_meal

    with get_conn() as conn:
        original = await _load_meal_response(conn, str(meal_id))
        profile, other_meals_kcal = _load_edit_context(conn, str(meal_id), str(body.user_id))

    meal_dict = original.model_dump(mode="json")
    result = await edit_meal(meal_dict, body.instruction, profile=profile, other_meals_kcal=other_meals_kcal)

    if result is None:
        raise HTTPException(status_code=422, detail="AI could not process the edit instruction")

    # Build a preview response by looking up proposed foods from cache (no meal_foods insert)
    with get_conn() as conn:
        proposed_foods: list[MealFoodResponse] = []
        for food_item in result.get("foods", []):
            fdc_id = food_item["fdc_id"]
            quantity_g = food_item["quantity_g"]
            try:
                food_data = await _get_or_cache_food(conn, fdc_id)
            except HTTPException:
                # AI hallucinated an FDC ID — fall back to searching by description
                description = food_item.get("description", "")
                if not description:
                    continue
                search_results = await usda_svc.search_foods(description, limit=1)
                if not search_results:
                    continue
                fdc_id = search_results[0].fdc_id
                food_data = await _get_or_cache_food(conn, fdc_id)
            conn.commit()
            scaled = usda_svc.scale_nutrients(food_data["nutrients"], float(quantity_g))
            proposed_foods.append(
                MealFoodResponse(
                    id=uuid.uuid4(),
                    external_id=f"mfood_{uuid.uuid4().hex[:12]}",
                    fdc_id=fdc_id,
                    description=food_data["description"],
                    quantity_g=float(quantity_g),
                    serving_description=food_item.get("serving_description"),
                    nutrients=scaled,
                )
            )

    return MealResponse(
        id=original.id,
        external_id=original.external_id,
        name=result.get("name") or original.name,
        meal_type=original.meal_type,
        scheduled_date=original.scheduled_date,
        instructions=result.get("instructions") or original.instructions,
        notes=original.notes,
        foods=proposed_foods,
        total_nutrition=sum_nutrients([f.nutrients for f in proposed_foods]),
    )


@router.post("/{meal_id}/ai-edit", response_model=MealResponse)
async def ai_edit_meal(meal_id: uuid.UUID, body: MealAIEditBody):
    from meal_planner.services.meal_edit import edit_meal

    with get_conn() as conn:
        meal = await _load_meal_response(conn, str(meal_id))
        profile, other_meals_kcal = _load_edit_context(conn, str(meal_id), str(body.user_id))

    meal_dict = meal.model_dump(mode="json")
    result = await edit_meal(meal_dict, body.instruction, profile=profile, other_meals_kcal=other_meals_kcal)

    if result is None:
        raise HTTPException(status_code=422, detail="AI could not process the edit instruction")

    # Apply the AI's update
    update_body = MealUpdateBody(
        name=result.get("name"),
        instructions=result.get("instructions"),
        foods=[
            MealFoodIn(
                fdc_id=f["fdc_id"],
                quantity_g=f["quantity_g"],
                serving_description=f.get("serving_description"),
            )
            for f in result.get("foods", [])
        ],
    )
    return await update_meal(meal_id, update_body)
