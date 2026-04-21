import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import get_conn
from meal_planner.models.favorite import FavoriteMealResponse, FavoriteMealSummary
from meal_planner.models.meal_plan import MealFoodResponse
from meal_planner.models.food import NutrientSummary
from meal_planner.services import usda as usda_svc
from meal_planner.services.nutrition import sum_nutrients

router = APIRouter(prefix="/favorites", tags=["favorites"])


class FavoriteMealBody(BaseModel):
    meal_id: uuid.UUID


def _load_favorite_response(conn, fav_id: str) -> FavoriteMealResponse:
    row = conn.execute(
        """
        SELECT id, user_id, name, meal_type, instructions, notes, source_meal_id, created_at
        FROM favorite_meals WHERE id = %s
        """,
        (fav_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Favorite not found")

    food_rows = conn.execute(
        """
        SELECT fmf.id, fmf.fdc_id, f.description, f.food_category, f.nutrients,
               fmf.quantity_g, fmf.serving_description
        FROM favorite_meal_foods fmf
        JOIN foods f ON f.fdc_id = fmf.fdc_id
        WHERE fmf.favorite_meal_id = %s
        """,
        (fav_id,),
    ).fetchall()

    food_responses: list[MealFoodResponse] = []
    for fr in food_rows:
        nutrients_per_100g = NutrientSummary(**(fr[4] if fr[4] else {}))
        scaled = usda_svc.scale_nutrients(nutrients_per_100g, float(fr[5]))
        food_responses.append(
            MealFoodResponse(
                id=fr[0],
                fdc_id=fr[1],
                description=fr[2],
                food_category=fr[3],
                quantity_g=float(fr[5]),
                serving_description=fr[6],
                nutrients=scaled,
            )
        )

    return FavoriteMealResponse(
        id=row[0],
        user_id=row[1],
        name=row[2],
        meal_type=row[3],
        instructions=row[4] or [],
        notes=row[5],
        source_meal_id=row[6],
        created_at=row[7],
        foods=food_responses,
        total_nutrition=sum_nutrients([f.nutrients for f in food_responses]),
    )


# NOTE: /summaries must be registered before /{favorite_id} to avoid FastAPI
# treating "summaries" as a UUID path parameter.

@router.get("/summaries", response_model=list[FavoriteMealSummary])
def list_favorite_summaries(user_id: uuid.UUID):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT fm.id, fm.name, fm.meal_type, fm.created_at, fm.source_meal_id,
                   COALESCE(SUM((f.nutrients->>'calories')::numeric * fmf.quantity_g / 100), 0) AS calories,
                   COALESCE(SUM((f.nutrients->>'protein_g')::numeric * fmf.quantity_g / 100), 0) AS protein_g,
                   COALESCE(SUM((f.nutrients->>'fat_g')::numeric * fmf.quantity_g / 100), 0) AS fat_g,
                   COALESCE(SUM((f.nutrients->>'carbs_g')::numeric * fmf.quantity_g / 100), 0) AS carbs_g,
                   COALESCE(SUM((f.nutrients->>'fiber_g')::numeric * fmf.quantity_g / 100), 0) AS fiber_g,
                   COALESCE(SUM((f.nutrients->>'sugar_g')::numeric * fmf.quantity_g / 100), 0) AS sugar_g,
                   COALESCE(SUM((f.nutrients->>'saturated_fat_g')::numeric * fmf.quantity_g / 100), 0) AS saturated_fat_g,
                   COALESCE(SUM((f.nutrients->>'cholesterol_mg')::numeric * fmf.quantity_g / 100), 0) AS cholesterol_mg,
                   COALESCE(SUM((f.nutrients->>'sodium_mg')::numeric * fmf.quantity_g / 100), 0) AS sodium_mg,
                   COALESCE(SUM((f.nutrients->>'calcium_mg')::numeric * fmf.quantity_g / 100), 0) AS calcium_mg,
                   COALESCE(SUM((f.nutrients->>'iron_mg')::numeric * fmf.quantity_g / 100), 0) AS iron_mg,
                   COALESCE(SUM((f.nutrients->>'magnesium_mg')::numeric * fmf.quantity_g / 100), 0) AS magnesium_mg,
                   COALESCE(SUM((f.nutrients->>'phosphorus_mg')::numeric * fmf.quantity_g / 100), 0) AS phosphorus_mg,
                   COALESCE(SUM((f.nutrients->>'potassium_mg')::numeric * fmf.quantity_g / 100), 0) AS potassium_mg,
                   COALESCE(SUM((f.nutrients->>'zinc_mg')::numeric * fmf.quantity_g / 100), 0) AS zinc_mg,
                   COALESCE(SUM((f.nutrients->>'vitamin_a_ug')::numeric * fmf.quantity_g / 100), 0) AS vitamin_a_ug,
                   COALESCE(SUM((f.nutrients->>'vitamin_c_mg')::numeric * fmf.quantity_g / 100), 0) AS vitamin_c_mg,
                   COALESCE(SUM((f.nutrients->>'vitamin_d_ug')::numeric * fmf.quantity_g / 100), 0) AS vitamin_d_ug,
                   COALESCE(SUM((f.nutrients->>'vitamin_e_mg')::numeric * fmf.quantity_g / 100), 0) AS vitamin_e_mg,
                   COALESCE(SUM((f.nutrients->>'vitamin_k_ug')::numeric * fmf.quantity_g / 100), 0) AS vitamin_k_ug,
                   COALESCE(SUM((f.nutrients->>'vitamin_b6_mg')::numeric * fmf.quantity_g / 100), 0) AS vitamin_b6_mg,
                   COALESCE(SUM((f.nutrients->>'vitamin_b12_ug')::numeric * fmf.quantity_g / 100), 0) AS vitamin_b12_ug,
                   COALESCE(SUM((f.nutrients->>'folate_ug')::numeric * fmf.quantity_g / 100), 0) AS folate_ug,
                   COALESCE(SUM((f.nutrients->>'thiamin_mg')::numeric * fmf.quantity_g / 100), 0) AS thiamin_mg,
                   COALESCE(SUM((f.nutrients->>'riboflavin_mg')::numeric * fmf.quantity_g / 100), 0) AS riboflavin_mg,
                   COALESCE(SUM((f.nutrients->>'niacin_mg')::numeric * fmf.quantity_g / 100), 0) AS niacin_mg
            FROM favorite_meals fm
            LEFT JOIN favorite_meal_foods fmf ON fmf.favorite_meal_id = fm.id
            LEFT JOIN foods f ON f.fdc_id = fmf.fdc_id
            WHERE fm.user_id = %s
            GROUP BY fm.id, fm.name, fm.meal_type, fm.created_at, fm.source_meal_id
            ORDER BY fm.created_at DESC
            """,
            (str(user_id),),
        ).fetchall()

    result = []
    for r in rows:
        nutrition = NutrientSummary(
            calories=float(r[5]),
            protein_g=float(r[6]),
            fat_g=float(r[7]),
            carbs_g=float(r[8]),
            fiber_g=float(r[9]),
            sugar_g=float(r[10]),
            saturated_fat_g=float(r[11]),
            cholesterol_mg=float(r[12]),
            sodium_mg=float(r[13]),
            calcium_mg=float(r[14]),
            iron_mg=float(r[15]),
            magnesium_mg=float(r[16]),
            phosphorus_mg=float(r[17]),
            potassium_mg=float(r[18]),
            zinc_mg=float(r[19]),
            vitamin_a_ug=float(r[20]),
            vitamin_c_mg=float(r[21]),
            vitamin_d_ug=float(r[22]),
            vitamin_e_mg=float(r[23]),
            vitamin_k_ug=float(r[24]),
            vitamin_b6_mg=float(r[25]),
            vitamin_b12_ug=float(r[26]),
            folate_ug=float(r[27]),
            thiamin_mg=float(r[28]),
            riboflavin_mg=float(r[29]),
            niacin_mg=float(r[30]),
        )
        result.append(FavoriteMealSummary(
            id=r[0],
            name=r[1],
            meal_type=r[2],
            created_at=r[3],
            source_meal_id=r[4],
            total_nutrition=nutrition,
        ))
    return result


@router.get("", response_model=list[FavoriteMealResponse])
def list_favorites(user_id: uuid.UUID):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM favorite_meals WHERE user_id = %s ORDER BY created_at DESC",
            (str(user_id),),
        ).fetchall()
        return [_load_favorite_response(conn, str(r[0])) for r in rows]


@router.post("", response_model=FavoriteMealResponse, status_code=201)
def favorite_meal(user_id: uuid.UUID, body: FavoriteMealBody):
    with get_conn() as conn:
        meal_row = conn.execute(
            "SELECT id, name, meal_type, instructions, notes FROM meals WHERE id = %s",
            (str(body.meal_id),),
        ).fetchone()
        if not meal_row:
            raise HTTPException(status_code=404, detail="Meal not found")

        food_rows = conn.execute(
            "SELECT fdc_id, quantity_g, serving_description FROM meal_foods WHERE meal_id = %s",
            (str(body.meal_id),),
        ).fetchall()

        fav_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO favorite_meals (id, user_id, name, meal_type, instructions, notes, source_meal_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(fav_id),
                str(user_id),
                meal_row[1],
                meal_row[2],
                meal_row[3] or [],
                meal_row[4],
                str(body.meal_id),
            ),
        )

        for fr in food_rows:
            conn.execute(
                """
                INSERT INTO favorite_meal_foods (id, favorite_meal_id, fdc_id, quantity_g, serving_description)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), str(fav_id), fr[0], fr[1], fr[2]),
            )

        conn.commit()
        return _load_favorite_response(conn, str(fav_id))


@router.get("/{favorite_id}", response_model=FavoriteMealResponse)
def get_favorite(favorite_id: uuid.UUID):
    with get_conn() as conn:
        return _load_favorite_response(conn, str(favorite_id))


@router.delete("/{favorite_id}", status_code=204)
def unfavorite_meal(favorite_id: uuid.UUID):
    with get_conn() as conn:
        result = conn.execute(
            "DELETE FROM favorite_meals WHERE id = %s RETURNING id",
            (str(favorite_id),),
        ).fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Favorite not found")
        conn.commit()
