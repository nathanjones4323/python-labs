import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from db import get_conn
from meal_planner.models.user import UserProfileResponse
from meal_planner.models.meal_plan import MealPlanCreate, MealIn, MealFoodIn
from meal_planner.routers.meal_plans import create_meal_plan
from meal_planner.services import ai as ai_svc
from datetime import date

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str


class ChatRequest(BaseModel):
    user_id: uuid.UUID
    message: str


def _load_profile(conn, user_id: uuid.UUID) -> UserProfileResponse | None:
    row = conn.execute(
        """
        SELECT user_id, dietary_restrictions, allergies, goals,
               calorie_target, meals_per_day, preferences, onboarding_complete
        FROM user_profiles WHERE user_id = %s
        """,
        (str(user_id),),
    ).fetchone()
    if not row:
        return None
    return UserProfileResponse(
        user_id=row[0],
        dietary_restrictions=row[1] or [],
        allergies=row[2] or [],
        goals=row[3],
        calorie_target=row[4],
        meals_per_day=row[5] or 3,
        preferences=row[6] or {},
        onboarding_complete=row[7],
    )


def _load_conversation(conn, user_id: uuid.UUID) -> tuple[uuid.UUID, list[dict]]:
    row = conn.execute(
        "SELECT id, messages FROM conversations WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
        (str(user_id),),
    ).fetchone()
    if row:
        return row[0], row[1] if row[1] else []
    convo_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO conversations (id, user_id, messages) VALUES (%s, %s, '[]'::jsonb)",
        (str(convo_id), str(user_id)),
    )
    conn.commit()
    return convo_id, []


def _save_conversation(conn, convo_id: uuid.UUID, messages: list[dict]) -> None:
    # Store only role+text content to avoid serializing tool blocks
    serializable = []
    for msg in messages:
        if isinstance(msg["content"], str):
            serializable.append({"role": msg["role"], "content": msg["content"]})
        elif isinstance(msg["content"], list):
            text_parts = [
                b["text"] for b in msg["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if text_parts:
                serializable.append({"role": msg["role"], "content": " ".join(text_parts)})
    conn.execute(
        "UPDATE conversations SET messages = %s::jsonb, updated_at = NOW() WHERE id = %s",
        (json.dumps(serializable), str(convo_id)),
    )
    conn.commit()


@router.post("/stream")
async def chat_stream(body: ChatRequest):
    with get_conn() as conn:
        # Verify user exists
        user = conn.execute(
            "SELECT id FROM users WHERE id = %s", (str(body.user_id),)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        profile = _load_profile(conn, body.user_id)
        convo_id, history = _load_conversation(conn, body.user_id)

    # Build messages list for Claude
    messages = history + [{"role": "user", "content": body.message}]

    async def event_stream():
        full_assistant_text = []
        plan_data = None

        async for chunk in ai_svc.chat_stream(messages, profile):
            yield chunk
            # Parse chunks to collect assistant text and plan
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                try:
                    payload = json.loads(chunk[6:])
                    if payload.get("type") == "text":
                        full_assistant_text.append(payload["content"])
                    elif payload.get("type") == "plan_created":
                        plan_data = payload["plan"]
                except (json.JSONDecodeError, KeyError):
                    pass

        # Persist the updated conversation
        updated_messages = messages + [
            {"role": "assistant", "content": "".join(full_assistant_text)}
        ]
        with get_conn() as conn:
            _save_conversation(conn, convo_id, updated_messages)

            # If the AI created a plan, persist it to the DB
            if plan_data:
                try:
                    plan_body = MealPlanCreate(
                        name=plan_data["name"],
                        start_date=date.fromisoformat(plan_data["start_date"]),
                        end_date=date.fromisoformat(plan_data["end_date"]),
                        meals=[
                            MealIn(
                                name=m["name"],
                                meal_type=m["meal_type"],
                                scheduled_date=date.fromisoformat(m["scheduled_date"]),
                                instructions=m.get("instructions", []),
                                notes=m.get("notes"),
                                foods=[
                                    MealFoodIn(
                                        fdc_id=f["fdc_id"],
                                        quantity_g=f["quantity_g"],
                                        serving_description=f.get("serving_description"),
                                    )
                                    for f in m.get("foods", [])
                                ],
                            )
                            for m in plan_data.get("meals", [])
                        ],
                    )
                    saved_plan = await create_meal_plan(body.user_id, plan_body)
                    yield f"data: {json.dumps({'type': 'plan_saved', 'plan_id': str(saved_plan.id)})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("/{user_id}/history", status_code=204)
def clear_history(user_id: uuid.UUID):
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET messages = '[]'::jsonb, updated_at = NOW() WHERE user_id = %s",
            (str(user_id),),
        )
        conn.commit()
