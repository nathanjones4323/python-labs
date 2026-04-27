import json
import uuid
from fastapi import APIRouter, HTTPException
from db import get_conn
from meal_planner.models.user import UserCreate, UserProfile, ProfilePatchRequest, UserProfileResponse, UserResponse
from meal_planner.services.extraction import deep_merge_preferences

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=201)
def create_user(body: UserCreate):
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO users (email) VALUES (%s) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id, external_id, email",
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
            "SELECT id, external_id, email FROM users WHERE id = %s", (str(user_id),)
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


@router.patch("/{user_id}/profile", response_model=UserProfileResponse)
def patch_profile(user_id: uuid.UUID, body: ProfilePatchRequest):
    with get_conn() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE id = %s", (str(user_id),)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        existing = conn.execute(
            """
            SELECT dietary_restrictions, allergies, goals, calorie_target,
                   meals_per_day, preferences, onboarding_complete
            FROM user_profiles WHERE user_id = %s
            """,
            (str(user_id),),
        ).fetchone()

        cur_dietary = existing[0] if existing else []
        cur_allergies = existing[1] if existing else []
        cur_goals = existing[2] if existing else None
        cur_calorie = existing[3] if existing else None
        cur_meals = existing[4] if existing else 3
        cur_prefs = existing[5] if existing else {}

        new_prefs = (
            deep_merge_preferences(cur_prefs or {}, body.preferences)
            if body.preferences
            else (cur_prefs or {})
        )

        row = conn.execute(
            """
            INSERT INTO user_profiles
                (user_id, dietary_restrictions, allergies, goals, calorie_target,
                 meals_per_day, preferences, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                dietary_restrictions = EXCLUDED.dietary_restrictions,
                allergies            = EXCLUDED.allergies,
                goals                = EXCLUDED.goals,
                calorie_target       = EXCLUDED.calorie_target,
                meals_per_day        = EXCLUDED.meals_per_day,
                preferences          = EXCLUDED.preferences,
                updated_at           = NOW()
            RETURNING user_id, dietary_restrictions, allergies, goals,
                      calorie_target, meals_per_day, preferences, onboarding_complete
            """,
            (
                str(user_id),
                body.dietary_restrictions if body.dietary_restrictions is not None else cur_dietary,
                body.allergies if body.allergies is not None else cur_allergies,
                body.goals if body.goals is not None else cur_goals,
                body.calorie_target if body.calorie_target is not None else cur_calorie,
                body.meals_per_day if body.meals_per_day is not None else cur_meals,
                json.dumps(new_prefs),
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
    return UserResponse(id=user_row[0], external_id=user_row[1], email=user_row[2], profile=profile)
