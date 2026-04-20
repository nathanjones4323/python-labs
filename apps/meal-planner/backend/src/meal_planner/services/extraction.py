"""
Background preference extraction.

After a conversation ends, extract preference signals from the transcript
and deep-merge them into user_profiles.preferences.

Called via FastAPI BackgroundTasks — runs in a thread pool after the response
is sent, so it is safe to open its own DB connection here.
"""

import copy
import json
import logging
import uuid
from datetime import datetime, timezone

import anthropic
from db import get_conn
from meal_planner.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_sync_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

EXTRACTION_SYSTEM_PROMPT = (
    "You are a preference extractor for a meal planning app.\n"
    "Given a conversation and the user's current preferences, identify any new signals: "
    "dislikes, favorites, cuisine preferences, feedback, or behavioral traits "
    "(cooking skill, time, budget, meal prep willingness).\n\n"
    "Return ONLY a raw JSON object — no explanation, no markdown, no preamble.\n"
    "Return {} if nothing new was found.\n\n"
    "Schema for dislikes/favorites:\n"
    '  { "item": "...", "confidence": "high|medium|low", "source": "explicit|feedback|behavioral", "since": "YYYY-MM-DD" }\n\n'
    "Schema for feedback_log entries:\n"
    '  { "item": "...", "sentiment": "positive|negative", "note": "...", "date": "YYYY-MM-DD" }'
)


def build_extraction_prompt(current_preferences: dict, messages: list[dict]) -> str:
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages
        if isinstance(m.get("content"), str)
    )
    return (
        f"Current preferences:\n{json.dumps(current_preferences, indent=2)}\n\n"
        f"Conversation:\n{transcript}\n\n"
        "Extract new signals as a JSON patch."
    )


def deep_merge_preferences(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)

    for key, value in patch.items():
        if key == "feedback_log":
            existing = result.get("feedback_log", [])
            existing.extend(value)
            result["feedback_log"] = existing[-50:]

        elif key == "behavioral" and isinstance(value, dict):
            result["behavioral"] = {**result.get("behavioral", {}), **value}

        elif key == "cuisine_preferences" and isinstance(value, list):
            existing = set(result.get("cuisine_preferences", []))
            result["cuisine_preferences"] = list(existing | set(value))

        elif (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "item" in value[0]
        ):
            # Upsert by item name (dislikes, favorites)
            existing = {e["item"]: e for e in result.get(key, [])}
            for entry in value:
                existing[entry["item"]] = entry
            result[key] = list(existing.values())

        else:
            result[key] = value

    result.setdefault("meta", {})["last_extracted_at"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return result


def run_extraction(conversation_id: uuid.UUID) -> None:
    """
    Entry point for BackgroundTasks.  Opens its own DB connection so the
    request connection lifecycle does not affect extraction.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT c.user_id, c.messages, c.extracted,
                   p.preferences
            FROM conversations c
            LEFT JOIN user_profiles p ON p.user_id = c.user_id
            WHERE c.id = %s
            """,
            (str(conversation_id),),
        ).fetchone()

        if not row:
            logger.warning("run_extraction: conversation %s not found", conversation_id)
            return

        user_id, messages, already_extracted, current_prefs = row

        if already_extracted:
            return

        if not messages:
            conn.execute(
                "UPDATE conversations SET extracted = TRUE WHERE id = %s",
                (str(conversation_id),),
            )
            conn.commit()
            return

        current_prefs = current_prefs or {}

        try:
            response = _sync_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=EXTRACTION_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": build_extraction_prompt(current_prefs, messages),
                }],
            )
            raw = response.content[0].text.strip()
        except Exception:
            logger.exception(
                "Extraction LLM call failed for conversation %s", conversation_id
            )
            conn.execute(
                "UPDATE conversations SET extracted = TRUE WHERE id = %s",
                (str(conversation_id),),
            )
            conn.commit()
            return

        try:
            patch = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Extraction produced invalid JSON for conversation %s: %.200s",
                conversation_id,
                raw,
            )
            patch = {}

        if patch:
            merged = deep_merge_preferences(current_prefs, patch)
            conn.execute(
                """
                INSERT INTO user_profiles (user_id, preferences, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET preferences = %s::jsonb,
                        updated_at = NOW()
                """,
                (str(user_id), json.dumps(merged), json.dumps(merged)),
            )

        conn.execute(
            "UPDATE conversations SET extracted = TRUE WHERE id = %s",
            (str(conversation_id),),
        )
        conn.commit()
        logger.info("Extraction complete for conversation %s", conversation_id)
