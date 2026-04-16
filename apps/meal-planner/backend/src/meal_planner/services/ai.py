"""
Claude-powered meal planning agent.

The agent has access to three tools:
  - search_foods: search USDA FDC for whole-food ingredients
  - get_food_nutrition: fetch nutrients for a specific food
  - create_meal_plan: persist a complete meal plan to the database

User dietary preferences are injected into the system prompt so Claude always
has them in context without re-asking the user each conversation.
"""

import json
import uuid
from typing import AsyncIterator

import anthropic
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
- After creating the plan, summarize the weekly nutritional totals for the user.{profile_section}"""


async def _handle_tool_call(
    tool_name: str,
    tool_input: dict,
    pending_plan: dict | None,
) -> tuple[str, dict | None]:
    """Execute a tool call and return (result_text, updated_pending_plan)."""
    if tool_name == "search_foods":
        results = await usda_svc.search_foods(
            tool_input["query"], tool_input.get("limit", 5)
        )
        return json.dumps([r.model_dump() for r in results]), pending_plan

    elif tool_name == "get_food_nutrition":
        food = await usda_svc.get_food(tool_input["fdc_id"])
        if not food:
            return json.dumps({"error": "Food not found"}), pending_plan
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
        # Return the plan data to the router to persist — we don't have DB access here
        return json.dumps({"status": "plan_ready", "data": tool_input}), tool_input

    return json.dumps({"error": f"Unknown tool: {tool_name}"}), pending_plan


async def chat_stream(
    messages: list[dict],
    profile: UserProfileResponse | None,
) -> AsyncIterator[str]:
    """
    Run the agent loop and yield SSE-formatted text chunks.
    Yields special __PLAN__:<json> line when a meal plan is created.
    """
    system = _build_system_prompt(profile)
    pending_plan: dict | None = None

    # Agentic loop: keep going until end_turn or no more tool calls
    while True:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Collect text and tool use blocks
        text_parts: list[str] = []
        tool_calls: list[anthropic.types.ToolUseBlock] = []

        for block in response.content:
            if isinstance(block, anthropic.types.TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, anthropic.types.ToolUseBlock):
                tool_calls.append(block)

        # Yield any text to the client
        for text in text_parts:
            yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"

        # Append assistant message
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_calls:
            break

        # Execute tool calls and build tool_result messages
        tool_results: list[dict] = []
        for tc in tool_calls:
            yield f"data: {json.dumps({'type': 'tool_use', 'name': tc.name})}\n\n"
            result_text, pending_plan = await _handle_tool_call(
                tc.name, tc.input, pending_plan
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result_text,
            })

        messages.append({"role": "user", "content": tool_results})

    # Signal if a plan was created so the router can persist it
    if pending_plan:
        yield f"data: {json.dumps({'type': 'plan_created', 'plan': pending_plan})}\n\n"

    yield "data: [DONE]\n\n"
