"""
Claude-powered meal planning agent with streaming.

The agent has access to three tools:
  - search_foods: search USDA FDC for whole-food ingredients
  - get_food_nutrition: fetch nutrients for a specific food
  - create_meal_plan: persist a complete meal plan to the database

Uses the streaming API so text appears word-by-word in the UI.
"""

import json
from typing import AsyncIterator

import anthropic
from db import get_conn
from meal_planner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from meal_planner.models.user import UserProfileResponse
from meal_planner.services import usda as usda_svc

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
        "name": "get_food_nutrition",
        "description": "Get full nutrient detail for a specific food by USDA FDC ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fdc_id": {"type": "integer"},
                "quantity_g": {
                    "type": "number",
                    "description": "Quantity in grams to calculate nutrition for",
                },
            },
            "required": ["fdc_id", "quantity_g"],
        },
    },
    {
        "name": "create_meal_plan",
        "description": (
            "Save a complete meal plan to the database. Call this once you have "
            "looked up all ingredient FDC IDs and confirmed their nutritional fit. "
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
                                        "quantity_g": {"type": "number"},
                                        "serving_description": {"type": "string"},
                                    },
                                    "required": ["fdc_id", "quantity_g"],
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
    "get_food_nutrition": "Looking up nutrition…",
    "create_meal_plan": "Saving meal plan…",
}


def _build_system_prompt(profile: UserProfileResponse | None) -> str:
    profile_section = ""
    if profile and profile.onboarding_complete:
        parts = []
        if profile.dietary_restrictions:
            parts.append(f"Dietary restrictions: {', '.join(profile.dietary_restrictions)}")
        if profile.allergies:
            parts.append(f"Allergies: {', '.join(profile.allergies)}")
        if profile.goals:
            parts.append(f"Health goals: {profile.goals}")
        if profile.calorie_target:
            parts.append(f"Daily calorie target: {profile.calorie_target} kcal")
        if profile.meals_per_day:
            parts.append(f"Meals per day: {profile.meals_per_day}")
        if profile.preferences:
            parts.append(f"Preferences: {json.dumps(profile.preferences)}")
        profile_section = "\n\nUser profile:\n" + "\n".join(parts)

    return f"""You are a meal planning assistant with access to the USDA FoodData Central database.

Your job is to help users create personalized meal plans with accurate nutritional information.

Guidelines:
- Use ONLY whole food ingredients from USDA Foundation Foods or SR Legacy data. Never suggest branded products.
- Always search for ingredient FDC IDs before creating a meal plan — do not invent FDC IDs.
- Provide detailed step-by-step cooking instructions for every meal.
- Quantities should be realistic (grams). Include a human-readable serving_description like "1 cup (240g)".
- Focus on nutritional accuracy — verify macros align with the user's goals.
- When creating a meal plan, include every ingredient with its FDC ID and gram weight.
- After creating the plan, summarize the daily nutritional totals for the user.{profile_section}"""


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


async def _handle_tool_call(
    tool_name: str,
    tool_input: dict,
    pending_plan: dict | None,
) -> tuple[str, dict | None]:
    if tool_name == "search_foods":
        results = await usda_svc.search_foods(
            tool_input["query"], tool_input.get("limit", 5)
        )
        # Cache every result so future get_food_nutrition calls are free
        for food in results:
            try:
                _cache_food(food)
            except Exception:
                pass  # non-fatal
        return json.dumps([r.model_dump() for r in results]), pending_plan

    elif tool_name == "get_food_nutrition":
        fdc_id = tool_input["fdc_id"]
        # Check Postgres cache first
        food = _get_cached_food(fdc_id)
        if food is None:
            food = await usda_svc.get_food(fdc_id)
            if not food:
                return json.dumps({"error": "Food not found"}), pending_plan
            try:
                _cache_food(food)
            except Exception:
                pass
        scaled = usda_svc.scale_nutrients(food.nutrients_per_100g, tool_input["quantity_g"])
        return (
            json.dumps({
                "fdc_id": food.fdc_id,
                "description": food.description,
                "quantity_g": tool_input["quantity_g"],
                "nutrients": scaled.model_dump(),
            }),
            pending_plan,
        )

    elif tool_name == "create_meal_plan":
        return json.dumps({"status": "plan_ready", "data": tool_input}), tool_input

    return json.dumps({"error": f"Unknown tool: {tool_name}"}), pending_plan


async def chat_stream(
    messages: list[dict],
    profile: UserProfileResponse | None,
) -> AsyncIterator[str]:
    """
    Streaming agentic loop. Yields SSE-formatted chunks:
      {"type": "text",       "content": "..."}          incremental text
      {"type": "tool_use",   "name": "...", "label": "..."}  tool starting
      {"type": "plan_created","plan": {...}}              plan ready to save
    """
    system = _build_system_prompt(profile)
    pending_plan: dict | None = None

    while True:
        async with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=8096,
            system=system,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if isinstance(event, anthropic.types.RawContentBlockStartEvent):
                    if isinstance(event.content_block, anthropic.types.ToolUseBlock):
                        label = _TOOL_LABELS.get(event.content_block.name, "Working…")
                        yield f"data: {json.dumps({'type': 'tool_use', 'name': event.content_block.name, 'label': label})}\n\n"

                elif isinstance(event, anthropic.types.RawContentBlockDeltaEvent):
                    if isinstance(event.delta, anthropic.types.TextDelta) and event.delta.text:
                        yield f"data: {json.dumps({'type': 'text', 'content': event.delta.text})}\n\n"

            final = await stream.get_final_message()

        # Append assistant turn to history
        messages.append({"role": "assistant", "content": final.content})

        # Check if we're done
        tool_use_blocks = [b for b in final.content if isinstance(b, anthropic.types.ToolUseBlock)]
        if final.stop_reason == "end_turn" or not tool_use_blocks:
            break

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
