"""
Claude-powered meal planning agent with streaming.

The agent has access to three tools:
  - search_foods: search USDA FDC for whole-food ingredients
  - get_food_nutrition: fetch nutrients for a specific food
  - create_meal_plan: persist a complete meal plan to the database

Uses the streaming API so text appears word-by-word in the UI.
"""

import json
import logging
from typing import AsyncIterator

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from db import get_conn
from meal_planner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL_PLANNING
from meal_planner.models.user import UserProfileResponse
from meal_planner.services import usda as usda_svc

logger = logging.getLogger("uvicorn.error")

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "search_foods",
        "description": (
            "Search the USDA FoodData Central database for whole food ingredients. "
            "Returns Foundation Foods and SR Legacy data — no branded products. "
            "Use this to find FDC IDs and nutrient data for ingredients."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Ingredient name to search"},
                "limit": {"type": "integer", "default": 5, "maximum": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_meal_plan",
        "description": (
            "Save a complete meal plan to the database. Call this once you have "
            "looked up all ingredient FDC IDs. Do NOT specify quantities — the system "
            "will compute optimal gram weights automatically. "
            "Always include step-by-step cooking instructions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "meals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "meal_type": {
                                "type": "string",
                                "enum": ["breakfast", "lunch", "dinner", "snack"],
                            },
                            "scheduled_date": {"type": "string", "format": "date"},
                            "instructions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Step-by-step recipe instructions",
                            },
                            "notes": {"type": "string"},
                            "foods": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "fdc_id": {"type": "integer"},
                                        "role": {
                                            "type": "string",
                                            "enum": ["protein", "carb", "vegetable", "fat", "other"],
                                            "description": "Nutritional role of this ingredient",
                                        },
                                        "serving_description": {"type": "string"},
                                    },
                                    "required": ["fdc_id", "role"],
                                },
                            },
                        },
                        "required": [
                            "name",
                            "meal_type",
                            "scheduled_date",
                            "instructions",
                            "foods",
                        ],
                    },
                },
            },
            "required": ["name", "start_date", "end_date", "meals"],
        },
    },
]

_TOOL_LABELS = {
    "search_foods": "Searching USDA database…",
    "create_meal_plan": "Saving meal plan…",
}


def _build_system_prompt(
    profile: UserProfileResponse | None,
    hearted_meals: list[dict] | None = None,
) -> str:
    has_profile = profile and profile.onboarding_complete

    if has_profile:
        p = profile.preferences or {}
        dislikes = [d["item"] for d in p.get("dislikes", [])]
        favorites = [f["item"] for f in p.get("favorites", [])]
        behavioral = p.get("behavioral", {})
        feedback_log = p.get("feedback_log", [])[-10:]

        recent_feedback = "\n".join(
            f"- {f['item']}: {f['sentiment']} — {f.get('note', '')}"
            for f in feedback_log
        ) or "None yet."

        profile_section = f"""

## User Profile
- Dietary restrictions: {', '.join(profile.dietary_restrictions) or 'None'}
- Allergies: {', '.join(profile.allergies) or 'None'}
- Goal: {profile.goals or 'Not set'} ({profile.calorie_target or '?'} kcal/day)
- Meals per day: {profile.meals_per_day}
- Dislikes: {', '.join(dislikes) or 'None'}
- Favorites: {', '.join(favorites) or 'None'}
- Cuisine preferences: {', '.join(p.get('cuisine_preferences', [])) or 'No preference'}
- Cooking skill: {behavioral.get('cooking_skill', 'Unknown')}
- Weekday cooking time: {behavioral.get('time_available_weekday_mins', '?')} min
- Weekend cooking time: {behavioral.get('time_available_weekend_mins', '?')} min
- Meal prep: {'Yes' if behavioral.get('meal_prep_willing') else 'No'}
- Leftovers OK: {'Yes' if behavioral.get('leftovers_ok', True) else 'No'}
- Budget: {behavioral.get('budget', 'Medium')}

## Recent Feedback
{recent_feedback}"""

        preferences_instruction = (
            "Always respect dietary restrictions and allergies absolutely. "
            "Apply dislikes unless the user explicitly says they want to try something. "
            "Prioritize favorites and cuisine preferences when making suggestions."
        )
    else:
        profile_section = ""
        preferences_instruction = (
            "The user has not set up a profile yet. "
            "Do NOT ask for dietary preferences, goals, or calorie targets inline. "
            "Build a sensible balanced plan from what the user requested. "
            "If you need more detail, suggest they update their profile in the Profile tab."
        )

    if hearted_meals:
        lines = [
            f"- {m['name']} ({m['meal_type']}, ~{round(m['total_nutrition']['calories'])} kcal, {round(m['total_nutrition']['protein_g'], 1)}g protein)"
            for m in hearted_meals[:20]
        ]
        hearted_section = (
            "\n\n## Hearted Meals (the user has saved these — incorporate them when possible)\n"
            + "\n".join(lines)
        )
    else:
        hearted_section = ""

    return f"""You are a meal planning assistant with access to the USDA FoodData Central database.

Your job is to help users create personalized meal plans. The system will automatically compute
optimal gram portions — your role is to select the right foods and assign each one a nutritional role.

Guidelines:
- Use ONLY whole food ingredients from USDA Foundation Foods or SR Legacy data. Never suggest branded products.
- Always search for ingredient FDC IDs before creating a meal plan — do not invent FDC IDs.
- Provide detailed step-by-step cooking instructions for every meal.
- Do NOT specify quantity_g values. Instead, assign each ingredient a role: protein, carb, vegetable, fat, or other.
- The system will compute portion sizes automatically to hit the user's calorie and macro targets.
- After creating the plan, briefly describe the meals and their nutritional focus for the user.
- When naming a plan, be specific: include duration and key characteristic — e.g. "High-Protein 5-Day", "Mediterranean Week", "Quick 30-Min Weeknight Meals". Avoid generic names like "7-Day Meal Plan".
- {preferences_instruction}{profile_section}{hearted_section}"""


def _cache_food(food: "usda_svc.FoodResult") -> None:
    """Upsert a food into the Postgres cache."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO foods (fdc_id, description, food_category, data_type, nutrients)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fdc_id) DO UPDATE
              SET description = EXCLUDED.description,
                  food_category = EXCLUDED.food_category,
                  nutrients = EXCLUDED.nutrients,
                  cached_at = NOW()
            """,
            (
                food.fdc_id,
                food.description,
                food.food_category,
                food.data_type,
                json.dumps(food.nutrients_per_100g.model_dump()),
            ),
        )
        conn.commit()


def _get_cached_food(fdc_id: int) -> "usda_svc.FoodResult | None":
    """Return a food from the Postgres cache, or None if not cached."""
    from meal_planner.models.food import FoodResult, NutrientSummary
    with get_conn() as conn:
        row = conn.execute(
            "SELECT fdc_id, description, food_category, data_type, nutrients FROM foods WHERE fdc_id = %s",
            (fdc_id,),
        ).fetchone()
    if not row:
        return None
    return FoodResult(
        fdc_id=row[0],
        description=row[1],
        food_category=row[2],
        data_type=row[3],
        nutrients_per_100g=NutrientSummary(**(row[4] or {})),
    )


def _local_search_foods(query: str, limit: int = 10) -> list:
    """Search the local foods table using Postgres full-text search.

    Falls back to a ILIKE prefix match if the FTS index returns no results
    (e.g. single-character queries or very unusual terms).
    """
    from meal_planner.models.food import FoodResult, NutrientSummary as NS
    with get_conn() as conn:
        # Primary: full-text search via tsvector column (requires migration 003)
        rows = conn.execute(
            """
            SELECT fdc_id, description, food_category, data_type, nutrients,
                   ts_rank(description_tsv, plainto_tsquery('english', %s)) AS rank
            FROM foods
            WHERE description_tsv @@ plainto_tsquery('english', %s)
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query, query, limit),
        ).fetchall()

        # Fallback: ILIKE if FTS returned nothing (single words, abbreviations, etc.)
        if not rows:
            rows = conn.execute(
                """
                SELECT fdc_id, description, food_category, data_type, nutrients, 0 AS rank
                FROM foods
                WHERE lower(description) LIKE lower(%s)
                ORDER BY description
                LIMIT %s
                """,
                (f"%{query}%", limit),
            ).fetchall()

    return [
        FoodResult(
            fdc_id=r[0],
            description=r[1],
            food_category=r[2],
            data_type=r[3],
            nutrients_per_100g=NS(**(r[4] or {})),
        )
        for r in rows
    ]


def _local_food_count() -> int:
    """Return number of foods in the local cache."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM foods").fetchone()
    return row[0] if row else 0


async def _handle_tool_call(
    tool_name: str,
    tool_input: dict,
    pending_plan: dict | None,
) -> tuple[str, dict | None]:
    if tool_name == "search_foods":
        query = tool_input["query"]
        limit = tool_input.get("limit", 5)

        # Use local DB when it has been pre-populated (>100 foods ingested)
        if _local_food_count() > 100:
            results = _local_search_foods(query, limit)
            # Fall back to USDA API if local search found nothing
            if not results:
                results = await usda_svc.search_foods(query, limit)
                for food in results:
                    try:
                        _cache_food(food)
                    except Exception:
                        pass
        else:
            results = await usda_svc.search_foods(query, limit)
            for food in results:
                try:
                    _cache_food(food)
                except Exception:
                    pass

        return json.dumps([r.model_dump() for r in results]), pending_plan

    elif tool_name == "create_meal_plan":
        return json.dumps({"status": "plan_ready", "data": tool_input}), tool_input

    return json.dumps({"error": f"Unknown tool: {tool_name}"}), pending_plan


async def chat_stream(
    messages: list[dict],
    profile: UserProfileResponse | None,
    hearted_meals: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Streaming agentic loop. Yields SSE-formatted chunks:
      {"type": "text",       "content": "..."}          incremental text (final turn only)
      {"type": "tool_use",   "name": "...", "label": "..."}  tool starting
      {"type": "plan_created","plan": {...}}              plan ready to save

    Text from intermediate tool-using turns is suppressed to avoid leaking
    Claude's internal planning monologue into the chat UI.
    """
    system = _build_system_prompt(profile, hearted_meals)
    pending_plan: dict | None = None

    turn = 0
    while True:
        turn += 1
        buffered_text: list[str] = []
        tool_sse: list[str] = []

        async with client.messages.stream(
            model=CLAUDE_MODEL_PLANNING,
            max_tokens=8096,
            system=system,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if isinstance(event, anthropic.types.RawContentBlockStartEvent):
                    if isinstance(event.content_block, ToolUseBlock):
                        label = _TOOL_LABELS.get(event.content_block.name, "Working…")
                        tool_sse.append(
                            f"data: {json.dumps({'type': 'tool_use', 'name': event.content_block.name, 'label': label})}\n\n"
                        )
                elif isinstance(event, anthropic.types.RawContentBlockDeltaEvent):
                    if isinstance(event.delta, anthropic.types.TextDelta) and event.delta.text:
                        buffered_text.append(event.delta.text)

            final = await stream.get_final_message()

        # Log every turn so we can see exactly which turn produces which text
        tool_use_blocks = [b for b in final.content if isinstance(b, ToolUseBlock)]
        text_blocks = [b for b in final.content if isinstance(b, TextBlock)]
        logger.warning(
            "[chat_stream] turn=%d stop_reason=%s tool_blocks=%d text_blocks=%d "
            "streamed_chars=%d content_block_types=%s | text_preview=%.300s",
            turn,
            final.stop_reason,
            len(tool_use_blocks),
            len(text_blocks),
            sum(len(c) for c in buffered_text),
            [type(b).__name__ for b in final.content],
            "".join(buffered_text)[:300],
        )

        messages.append({"role": "assistant", "content": final.content})

        if final.stop_reason == "end_turn" or not tool_use_blocks:
            # Final turn — extract text only from TextBlock content (not ThinkingBlock etc.)
            # Using final.content is the source of truth; buffered_text is equivalent but
            # listed here for clarity on which SDK type we're filtering.
            logger.warning("[chat_stream] → FINAL turn, yielding text")
            for chunk in buffered_text:
                yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"
            break

        # Intermediate turn — emit tool events only; discard planning text
        logger.warning("[chat_stream] → INTERMEDIATE turn, discarding text")
        for sse in tool_sse:
            yield sse

        # Run tool calls, collect results
        tool_results: list[dict] = []
        for block in tool_use_blocks:
            result_text, pending_plan = await _handle_tool_call(
                block.name, block.input, pending_plan
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        messages.append({"role": "user", "content": tool_results})

    if pending_plan:
        yield f"data: {json.dumps({'type': 'plan_created', 'plan': pending_plan})}\n\n"
