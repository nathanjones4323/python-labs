"""
Claude-powered meal planning agent with streaming.

The agent has access to three tools:
  - search_foods: search USDA Postgres cache for a single ingredient
  - batch_search_foods: search for multiple ingredients at once (preferred for plan creation)
  - create_meal_plan: persist a complete meal plan to the database

Uses the streaming API so text appears word-by-word in the UI.
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from db import get_conn
from meal_planner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL_PLANNING
from meal_planner.models.user import UserProfileResponse

logger = logging.getLogger("uvicorn.error")

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "search_foods",
        "description": (
            "Search the USDA FoodData Central database for a single ingredient. "
            "Returns Foundation Foods and SR Legacy data — no branded products. "
            "Use this for single lookups outside of meal plan creation. "
            "When building a meal plan, use batch_search_foods instead."
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
        "name": "batch_search_foods",
        "description": (
            "Search the USDA FoodData Central database for multiple ingredients at once. "
            "Use this when building a meal plan — pass ALL ingredient names you need in one call "
            "instead of calling search_foods repeatedly. "
            "Returns a dict mapping each query to its list of results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of ingredient names to search",
                    "maxItems": 30,
                },
                "limit": {"type": "integer", "default": 3, "maximum": 5},
            },
            "required": ["queries"],
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
    "batch_search_foods": "Searching ingredients…",
    "create_meal_plan": "Saving meal plan…",
}


def _build_system_prompt(
    profile: UserProfileResponse | None,
    hearted_meals: list[dict] | None = None,
    recent_meals: list[dict] | None = None,
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

    if recent_meals:
        grouped: dict[str, list[str]] = {}
        for m in recent_meals:
            grouped.setdefault(m["meal_type"], []).append(m["name"])
        lines = []
        for mtype in ["breakfast", "lunch", "dinner", "snack"]:
            if mtype in grouped:
                lines.append(f"  {mtype.capitalize()}: " + ", ".join(grouped[mtype]))
        recent_section = (
            "\n\n## Recently Generated Meals (avoid repeating these — create new variety)\n"
            + "\n".join(lines)
        )
    else:
        recent_section = ""

    return f"""You are a meal planning assistant with access to the USDA FoodData Central database.

Your job is to help users create personalized meal plans. The system will automatically compute
optimal gram portions — your role is to select the right foods and assign each one a nutritional role.

Guidelines:
- EFFICIENCY: When creating a meal plan, use batch_search_foods to look up ALL ingredients at once in a single call, then call create_meal_plan. Never call search_foods one ingredient at a time when building a plan. Use search_foods only for single-food lookups outside of plan creation.
- VARIETY (critical): When a user provides a list of ingredients, treat them as preferred staples — use them as primary ingredients but incorporate additional complementary foods (different grains, herbs, legumes, seasonal vegetables) to create diverse meals. Never build an entire plan using only the listed ingredients.
- Rotate primary proteins across consecutive meals of the same type — never use the same protein for more than 2 consecutive breakfasts, lunches, or dinners in a row.
- For plans of 5+ days, use at least 3 distinct protein sources, 4 distinct vegetables, and 2 distinct grain/starch types.
- Vary cuisines across the week (e.g. Mediterranean, Asian, Mexican, American, Indian) — aim for at least 3 distinct styles in a 7-day plan.
- Vary cooking methods (grilled, baked, stir-fried, steamed, slow-cooked, raw) — no single method for more than 40% of meals.
- Use ONLY whole food ingredients from USDA Foundation Foods or SR Legacy data. Never suggest branded products.
- Always search for ingredient FDC IDs before creating a meal plan — do not invent FDC IDs.
- Provide detailed step-by-step cooking instructions for every meal.
- Do NOT specify quantity_g values. Instead, assign each ingredient a role: protein, carb, vegetable, fat, or other.
- The system will compute portion sizes automatically to hit the user's calorie and macro targets.
- After creating the plan, briefly describe the meals and their nutritional focus for the user.
- When naming a plan, be specific: include duration and key characteristic — e.g. "High-Protein 5-Day", "Mediterranean Week", "Quick 30-Min Weeknight Meals". Avoid generic names like "7-Day Meal Plan".
- {preferences_instruction}{profile_section}{hearted_section}{recent_section}"""


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


async def _handle_tool_call(
    tool_name: str,
    tool_input: dict,
    pending_plan: dict | None,
) -> tuple[str, dict | None]:
    if tool_name == "search_foods":
        query = tool_input["query"]
        limit = tool_input.get("limit", 5)
        results = _local_search_foods(query, limit)
        return json.dumps([r.model_dump() for r in results]), pending_plan

    elif tool_name == "batch_search_foods":
        queries = tool_input["queries"]
        limit = tool_input.get("limit", 3)
        output = {q: [r.model_dump() for r in _local_search_foods(q, limit)] for q in queries}
        return json.dumps(output), pending_plan

    elif tool_name == "create_meal_plan":
        return json.dumps({"status": "plan_ready", "data": tool_input}), tool_input

    return json.dumps({"error": f"Unknown tool: {tool_name}"}), pending_plan


async def chat_stream(
    messages: list[dict],
    profile: UserProfileResponse | None,
    hearted_meals: list[dict] | None = None,
    recent_meals: list[dict] | None = None,
) -> AsyncIterator[str]:
    """
    Streaming agentic loop. Yields SSE-formatted chunks:
      {"type": "text",       "content": "..."}          incremental text (final turn only)
      {"type": "tool_use",   "name": "...", "label": "..."}  tool starting
      {"type": "plan_created","plan": {...}}              plan ready to save

    Text from intermediate tool-using turns is suppressed to avoid leaking
    Claude's internal planning monologue into the chat UI.
    """
    system = _build_system_prompt(profile, hearted_meals, recent_meals)
    pending_plan: dict | None = None

    MAX_TURNS = 25
    turn = 0
    while True:
        if turn >= MAX_TURNS:
            logger.error("[chat_stream] hit MAX_TURNS=%d, aborting loop", MAX_TURNS)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Reached maximum planning turns. Please try a shorter plan.'})}\n\n"
            break
        turn += 1
        buffered_text: list[str] = []
        tool_sse: list[str] = []

        _MAX_RETRIES = 3
        final = None
        for _attempt in range(_MAX_RETRIES + 1):
            try:
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
                break  # success
            except anthropic.RateLimitError:
                if _attempt >= _MAX_RETRIES:
                    raise
                wait = (2 ** _attempt) * 5  # 5s, 10s, 20s
                logger.warning(
                    "[chat_stream] RateLimitError turn=%d attempt=%d, retrying in %ds",
                    turn, _attempt, wait,
                )
                buffered_text = []
                tool_sse = []
                await asyncio.sleep(wait)

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
