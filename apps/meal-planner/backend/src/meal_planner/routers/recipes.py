import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import get_conn
from meal_planner.models.favorite import FavoriteMealResponse
from meal_planner.routers.favorites import _load_favorite_response
from meal_planner.services import recipe_ai

router = APIRouter(prefix="/recipes", tags=["recipes"])


class RecipeFoodInput(BaseModel):
    fdc_id: int
    quantity_g: float
    serving_description: str | None = None


class CreateRecipeBody(BaseModel):
    name: str
    meal_type: str
    instructions: list[str] = []
    notes: str | None = None
    foods: list[RecipeFoodInput]


class DescribeRecipeBody(BaseModel):
    user_id: uuid.UUID
    description: str


class ImageRecipeBody(BaseModel):
    user_id: uuid.UUID
    image_base64: str
    media_type: str = "image/jpeg"


def _insert_favorite(conn, user_id: str, body: CreateRecipeBody) -> str:
    if body.meal_type not in ("breakfast", "lunch", "dinner", "snack"):
        raise HTTPException(status_code=422, detail="Invalid meal_type")

    known_ids = {
        r[0]
        for r in conn.execute(
            "SELECT fdc_id FROM foods WHERE fdc_id = ANY(%s)",
            ([f.fdc_id for f in body.foods],),
        ).fetchall()
    }
    unknown = [f.fdc_id for f in body.foods if f.fdc_id not in known_ids]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fdc_ids: {unknown}")

    fav_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO favorite_meals (id, user_id, name, meal_type, instructions, notes, source_meal_id)
        VALUES (%s, %s, %s, %s, %s, %s, NULL)
        """,
        (fav_id, user_id, body.name, body.meal_type, body.instructions, body.notes),
    )
    for food in body.foods:
        conn.execute(
            """
            INSERT INTO favorite_meal_foods (id, favorite_meal_id, fdc_id, quantity_g, serving_description)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), fav_id, food.fdc_id, food.quantity_g, food.serving_description),
        )
    conn.commit()
    return fav_id


@router.post("", response_model=FavoriteMealResponse, status_code=201)
def create_recipe(user_id: uuid.UUID, body: CreateRecipeBody):
    with get_conn() as conn:
        fav_id = _insert_favorite(conn, str(user_id), body)
        return _load_favorite_response(conn, fav_id)


@router.post("/from-description")
def recipe_from_description(body: DescribeRecipeBody):
    try:
        return recipe_ai.analyze_description(body.description)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/from-image")
def recipe_from_image(body: ImageRecipeBody):
    try:
        return recipe_ai.analyze_image(body.image_base64, body.media_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
