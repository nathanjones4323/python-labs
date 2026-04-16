import uuid
from fastapi import APIRouter, HTTPException
from db import get_conn
from meal_planner.models.user import UserCreate, UserProfile, UserProfileResponse, UserResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=201)
def create_user(body: UserCreate):
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO users (email) VALUES (%s) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id, email",
            (body.email,),
        ).fetchone()
        profile_row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = %s", (row[0],)
        ).fetchone()
        conn.commit()
    return _build_user_response(row, profile_row)


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: uuid.UUID):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE id = %s", (str(user_id),)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        profile_row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = %s", (str(user_id),)
        ).fetchone()
    return _build_user_response(row, profile_row)


@router.put("/{user_id}/profile", response_model=UserProfileResponse)
def upsert_profile(user_id: uuid.UUID, body: UserProfile):
    with get_conn() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = %s", (str(user_id),)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        import json
        row = conn.execute(
            """
            INSERT INTO user_profiles
                (user_id, dietary_restrictions, allergies, goals, calorie_target, meals_per_day, preferences, onboarding_complete)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET
                dietary_restrictions = EXCLUDED.dietary_restrictions,
                allergies = EXCLUDED.allergies,
                goals = EXCLUDED.goals,
                calorie_target = EXCLUDED.calorie_target,
                meals_per_day = EXCLUDED.meals_per_day,
                preferences = EXCLUDED.preferences,
                onboarding_complete = TRUE,
                updated_at = NOW()
            RETURNING user_id, dietary_restrictions, allergies, goals, calorie_target, meals_per_day, preferences, onboarding_complete
            """,
            (
                str(user_id),
                body.dietary_restrictions,
                body.allergies,
                body.goals,
                body.calorie_target,
                body.meals_per_day,
                json.dumps(body.preferences),
            ),
        ).fetchone()
        conn.commit()

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


def _build_user_response(user_row, profile_row) -> UserResponse:
    profile = None
    if profile_row:
        profile = UserProfileResponse(
            user_id=profile_row[0],
            dietary_restrictions=profile_row[1] or [],
            allergies=profile_row[2] or [],
            goals=profile_row[3],
            calorie_target=profile_row[4],
            meals_per_day=profile_row[5] or 3,
            preferences=profile_row[6] or {},
            onboarding_complete=profile_row[7],
        )
    return UserResponse(id=user_row[0], email=user_row[1], profile=profile)
