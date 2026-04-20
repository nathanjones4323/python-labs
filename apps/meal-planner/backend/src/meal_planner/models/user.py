from pydantic import BaseModel, EmailStr
from typing import Any
import uuid


class UserCreate(BaseModel):
    email: EmailStr


class UserProfile(BaseModel):
    dietary_restrictions: list[str] = []
    allergies: list[str] = []
    goals: str | None = None
    calorie_target: int | None = None
    meals_per_day: int = 3
    preferences: dict[str, Any] = {}


class ProfilePatchRequest(BaseModel):
    dietary_restrictions: list[str] | None = None
    allergies: list[str] | None = None
    goals: str | None = None
    calorie_target: int | None = None
    meals_per_day: int | None = None
    preferences: dict[str, Any] | None = None  # deep-merged, not overwritten


class UserProfileResponse(UserProfile):
    user_id: uuid.UUID
    onboarding_complete: bool


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    profile: UserProfileResponse | None = None
