import uuid
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import get_conn
from meal_planner.config import GOOGLE_CLIENT_ID, GOOGLE_IOS_CLIENT_ID, DEV_MODE
from meal_planner.routers.users import _build_user_response

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleAuthRequest(BaseModel):
    access_token: str


@router.post("/google")
async def google_sign_in(body: GoogleAuthRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google auth not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_TOKENINFO_URL,
            params={"access_token": body.access_token},
        )

        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid Google token")

        info = resp.json()

        # Verify the token was issued for our app (web or iOS client)
        allowed_clients = {c for c in (GOOGLE_CLIENT_ID, GOOGLE_IOS_CLIENT_ID) if c}
        token_clients = {info.get("azp"), info.get("aud")} - {None}
        if not allowed_clients.intersection(token_clients):
            raise HTTPException(status_code=401, detail="Token was not issued for this app")

        if not info.get("email_verified", False):
            raise HTTPException(status_code=401, detail="Google email not verified")

        google_id: str = info["sub"]
        email: str = info["email"]

        # Fetch display name from userinfo (tokeninfo doesn't include name)
        ui_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {body.access_token}"},
        )
    display_name: str | None = ui_resp.json().get("name") if ui_resp.status_code == 200 else None

    with get_conn() as conn:
        # Try lookup by google_id first
        row = conn.execute(
            "SELECT id, email FROM users WHERE google_id = %s", (google_id,)
        ).fetchone()

        if row:
            user_id = row[0]
        else:
            # Fall back to email match (merges legacy email-only accounts)
            row = conn.execute(
                "SELECT id, email FROM users WHERE email = %s", (email,)
            ).fetchone()
            if row:
                user_id = row[0]
                conn.execute(
                    "UPDATE users SET google_id = %s, display_name = COALESCE(display_name, %s) WHERE id = %s",
                    (google_id, display_name, str(user_id)),
                )
            else:
                # New user
                row = conn.execute(
                    "INSERT INTO users (email, google_id, display_name) VALUES (%s, %s, %s) RETURNING id, email",
                    (email, google_id, display_name),
                ).fetchone()
                user_id = row[0]
            conn.commit()

        profile_row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = %s", (str(user_id),)
        ).fetchone()

    user_response = _build_user_response((user_id, email), profile_row)
    is_new_user = user_response.profile is None or not user_response.profile.onboarding_complete

    return {
        "user_id": str(user_response.id),
        "email": user_response.email,
        "display_name": display_name,
        "is_new_user": is_new_user,
        "profile": user_response.profile,
    }


@router.post("/dev-bypass")
async def dev_bypass_sign_in():
    """Dev-only endpoint to skip OAuth. Only active when DEV_MODE=true."""
    if not DEV_MODE:
        raise HTTPException(status_code=404, detail="Not found")

    dev_email = "dev@localhost"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE email = %s", (dev_email,)
        ).fetchone()
        if not row:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (user_id, dev_email, "Dev User"),
            )
            conn.commit()
            row = (user_id, dev_email)

        profile_row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = %s", (str(row[0]),)
        ).fetchone()

    user_response = _build_user_response(row, profile_row)
    is_new_user = user_response.profile is None or not user_response.profile.onboarding_complete

    return {
        "user_id": str(user_response.id),
        "email": user_response.email,
        "display_name": "Dev User",
        "is_new_user": is_new_user,
        "profile": user_response.profile,
    }
