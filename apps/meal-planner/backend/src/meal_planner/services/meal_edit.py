"""
Claude-powered meal editing agent.

Given a meal and a plain-English instruction, Claude searches for ingredient
replacements, verifies nutrition, then calls `update_meal` to return the
revised foods list.
"""

import json
import anthropic
from meal_planner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from meal_planner.services import usda as usda_svc

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

TOOLS: list[anthropic.types.ToolParam] = [
    {
        "name": "search_foods",
        "description": (
            "Search the USDA FoodData Central database for whole food ingredients. "
            "Returns Foundation Foods and SR Legacy data only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
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
                "quantity_g": {"type": "number"},
            },
            "required": ["fdc_id", "quantity_g"],
        },
    },
    {
        "name": "update_meal",
        "description": (
            "Apply the edited ingredient list to the meal. Call this once you have "
            "confirmed the FDC IDs and gram quantities for all ingredients. "
            "Include ALL ingredients (unchanged ones too)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Meal name (can be unchanged)"},
                "instructions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Updated step-by-step instructions reflecting any ingredient changes",
                },
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
            "required": ["name", "instructions", "foods"],
        },
    },
]


async def _handle_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "search_foods":
        results = await usda_svc.search_foods(tool_input["query"], tool_input.get("limit", 5))
        return json.dumps([r.model_dump() for r in results])

    elif tool_name == "get_food_nutrition":
        from meal_planner.services.ai import _get_cached_food, _cache_food
        fdc_id = tool_input["fdc_id"]
        food = _get_cached_food(fdc_id)
        if food is None:
            food = await usda_svc.get_food(fdc_id)
            if not food:
                return json.dumps({"error": "Food not found"})
            try:
                _cache_food(food)
            except Exception:
                pass
        scaled = usda_svc.scale_nutrients(food.nutrients_per_100g, tool_input["quantity_g"])
        return json.dumps({
            "fdc_id": food.fdc_id,
            "description": food.description,
            "quantity_g": tool_input["quantity_g"],
            "nutrients": scaled.model_dump(),
        })

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def edit_meal(
    meal_json: dict,
    instruction: str,
) -> dict | None:
    """
    Run a Claude agentic loop to edit a meal based on a plain-English instruction.
    Returns the `update_meal` tool input dict, or None if Claude didn't call update_meal.
    """
    system = (
        "You are a meal editing assistant with access to the USDA FoodData Central database.\n\n"
        "The user wants to modify a meal. Apply the requested change precisely — only swap or "
        "adjust what's asked. Keep unchanged ingredients intact.\n\n"
        "Steps:\n"
        "1. Understand what needs to change.\n"
        "2. Search USDA for any new ingredients needed.\n"
        "3. Look up nutrition for any new ingredients.\n"
        "4. Call update_meal with the complete revised foods list (all ingredients)."
    )

    meal_summary = json.dumps({
        "name": meal_json.get("name"),
        "meal_type": meal_json.get("meal_type"),
        "instructions": meal_json.get("instructions", []),
        "foods": [
            {
                "fdc_id": f["fdc_id"],
                "description": f["description"],
                "quantity_g": f["quantity_g"],
                "serving_description": f.get("serving_description"),
            }
            for f in meal_json.get("foods", [])
        ],
    }, indent=2)

    messages = [
        {
            "role": "user",
            "content": f"Current meal:\n{meal_summary}\n\nEdit instruction: {instruction}",
        }
    ]

    result: dict | None = None

    while True:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if isinstance(b, anthropic.types.ToolUseBlock)]

        if response.stop_reason == "end_turn" or not tool_use_blocks:
            break

        tool_results = []
        for block in tool_use_blocks:
            if block.name == "update_meal":
                result = block.input
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"status": "applied"}),
                })
            else:
                result_text = await _handle_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

        messages.append({"role": "user", "content": tool_results})

        # Stop once update_meal was called
        if result is not None:
            break

    return result
