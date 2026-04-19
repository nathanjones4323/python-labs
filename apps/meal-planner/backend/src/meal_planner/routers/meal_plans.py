import json
import uuid
from fastapi import APIRouter, HTTPException
from db import get_conn
from meal_planner.models.meal_plan import (
    MealPlanCreate,
    MealPlanResponse,
    MealPlanSummary,
    MealResponse,
    MealFoodResponse,
)
from meal_planner.models.food import NutrientSummary
from meal_planner.services import usda as usda_svc
from meal_planner.services.nutrition import sum_nutrients

router = APIRouter(prefix="/meal-plans", tags=["meal-plans"])


@router.get("", response_model=list[MealPlanSummary])
def list_meal_plans(user_id: uuid.UUID):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT mp.id, mp.name, mp.start_date, mp.end_date,
                   COUNT(m.id) AS meal_count
            FROM meal_plans mp
            LEFT JOIN meals m ON m.meal_plan_id = mp.id
            WHERE mp.user_id = %s
            GROUP BY mp.id, mp.name, mp.start_date, mp.end_date
            ORDER BY mp.start_date DESC
            """,
            (str(user_id),),
        ).fetchall()
    return [
        MealPlanSummary(
            id=r[0], name=r[1], start_date=r[2], end_date=r[3], meal_count=r[4]
        )
        for r in rows
    ]


@router.post("", response_model=MealPlanResponse, status_code=201)
async def create_meal_plan(user_id: uuid.UUID, body: MealPlanCreate):
    plan_id = uuid.uuid4()
    meal_responses: list[MealResponse] = []

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meal_plans (id, user_id, name, start_date, end_date) VALUES (%s, %s, %s, %s, %s)",
            (str(plan_id), str(user_id), body.name, body.start_date, body.end_date),
        )

        for meal_in in body.meals:
            meal_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO meals (id, meal_plan_id, name, meal_type, scheduled_date, instructions, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(meal_id),
                    str(plan_id),
                    meal_in.name,
                    meal_in.meal_type,
                    meal_in.scheduled_date,
                    meal_in.instructions,
                    meal_in.notes,
                ),
            )

            food_responses: list[MealFoodResponse] = []
            for food_in in meal_in.foods:
                food = await _get_or_cache_food(conn, food_in.fdc_id)
                scaled = usda_svc.scale_nutrients(food["nutrients"], food_in.quantity_g)

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
                food_responses.append(
                    MealFoodResponse(
                        id=mf_id,
                        fdc_id=food_in.fdc_id,
                        description=food["description"],
                        quantity_g=food_in.quantity_g,
                        serving_description=food_in.serving_description,
                        nutrients=scaled,
                    )
                )

            meal_nutrition = sum_nutrients([f.nutrients for f in food_responses])
            meal_responses.append(
                MealResponse(
                    id=meal_id,
                    name=meal_in.name,
                    meal_type=meal_in.meal_type,
                    scheduled_date=meal_in.scheduled_date,
                    instructions=meal_in.instructions,
                    notes=meal_in.notes,
                    foods=food_responses,
                    total_nutrition=meal_nutrition,
                )
            )

        conn.commit()

    plan_nutrition = sum_nutrients([m.total_nutrition for m in meal_responses])
    return MealPlanResponse(
        id=plan_id,
        name=body.name,
        start_date=body.start_date,
        end_date=body.end_date,
        meals=meal_responses,
        total_nutrition=plan_nutrition,
    )


@router.get("/{plan_id}", response_model=MealPlanResponse)
async def get_meal_plan(plan_id: uuid.UUID):
    with get_conn() as conn:
        plan_row = conn.execute(
            "SELECT id, name, start_date, end_date FROM meal_plans WHERE id = %s",
            (str(plan_id),),
        ).fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="Meal plan not found")

        meal_rows = conn.execute(
            """
            SELECT id, name, meal_type, scheduled_date, instructions, notes
            FROM meals WHERE meal_plan_id = %s
            ORDER BY scheduled_date,
              CASE meal_type
                WHEN 'breakfast' THEN 0
                WHEN 'snack'     THEN 1
                WHEN 'lunch'     THEN 2
                WHEN 'dinner'    THEN 3
                ELSE 4
              END
            """,
            (str(plan_id),),
        ).fetchall()

        meal_responses: list[MealResponse] = []
        for mr in meal_rows:
            meal_id = mr[0]
            food_rows = conn.execute(
                """
                SELECT mf.id, mf.fdc_id, f.description, f.nutrients, mf.quantity_g, mf.serving_description
                FROM meal_foods mf JOIN foods f ON f.fdc_id = mf.fdc_id
                WHERE mf.meal_id = %s
                """,
                (str(meal_id),),
            ).fetchall()

            food_responses: list[MealFoodResponse] = []
            for fr in food_rows:
                nutrients_per_100g = NutrientSummary(**(fr[3] if fr[3] else {}))
                scaled = usda_svc.scale_nutrients(nutrients_per_100g, float(fr[4]))
                food_responses.append(
                    MealFoodResponse(
                        id=fr[0],
                        fdc_id=fr[1],
                        description=fr[2],
                        quantity_g=float(fr[4]),
                        serving_description=fr[5],
                        nutrients=scaled,
                    )
                )

            meal_responses.append(
                MealResponse(
                    id=meal_id,
                    name=mr[1],
                    meal_type=mr[2],
                    scheduled_date=mr[3],
                    instructions=mr[4] or [],
                    notes=mr[5],
                    foods=food_responses,
                    total_nutrition=sum_nutrients([f.nutrients for f in food_responses]),
                )
            )

    plan_nutrition = sum_nutrients([m.total_nutrition for m in meal_responses])
    return MealPlanResponse(
        id=plan_row[0],
        name=plan_row[1],
        start_date=plan_row[2],
        end_date=plan_row[3],
        meals=meal_responses,
        total_nutrition=plan_nutrition,
    )


@router.delete("/{plan_id}", status_code=204)
def delete_meal_plan(plan_id: uuid.UUID):
    with get_conn() as conn:
        result = conn.execute(
            "DELETE FROM meal_plans WHERE id = %s", (str(plan_id),)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Meal plan not found")


async def _get_or_cache_food(conn, fdc_id: int) -> dict:
    """Return food from DB cache or fetch from USDA and cache it."""
    row = conn.execute(
        "SELECT description, nutrients FROM foods WHERE fdc_id = %s", (fdc_id,)
    ).fetchone()
    if row:
        return {
            "description": row[0],
            "nutrients": NutrientSummary(**(row[1] if row[1] else {})),
        }

    food = await usda_svc.get_food(fdc_id)
    if not food:
        raise HTTPException(status_code=422, detail=f"USDA food {fdc_id} not found")

    import json
    conn.execute(
        """
        INSERT INTO foods (fdc_id, description, food_category, data_type, nutrients)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (fdc_id) DO UPDATE SET nutrients = EXCLUDED.nutrients, cached_at = NOW()
        """,
        (
            food.fdc_id,
            food.description,
            food.food_category,
            food.data_type,
            json.dumps(food.nutrients_per_100g.model_dump()),
        ),
    )
    return {"description": food.description, "nutrients": food.nutrients_per_100g}
