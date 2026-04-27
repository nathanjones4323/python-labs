"""
AI-powered recipe analysis — extracts structured recipe data from a text
description or an image using Claude.

Neither function saves to the database; callers are responsible for persisting
the returned RecipeAIResult after the user reviews and confirms it.
"""

import json
import logging

import anthropic
from meal_planner.config import ANTHROPIC_API_KEY, CLAUDE_MODEL_PLANNING
from meal_planner.services.ai import _local_search_foods

logger = logging.getLogger("uvicorn.error")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_SEARCH_TOOL: anthropic.types.ToolParam = {
    "name": "search_foods",
    "description": (
        "Search the USDA FoodData Central database for whole food ingredients. "
        "Returns Foundation Foods and SR Legacy data — no branded products. "
        "Use this to find FDC IDs for each ingredient in the recipe."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Ingredient name to search"},
            "limit": {"type": "integer", "default": 5, "maximum": 10},
        },
        "required": ["query"],
    },
}

_FINALIZE_TOOL: anthropic.types.ToolParam = {
    "name": "finalize_recipe",
    "description": (
        "Call this once you have looked up all ingredient FDC IDs. "
        "Provide realistic single-serving gram quantities for each ingredient."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Recipe name"},
            "meal_type": {
                "type": "string",
                "enum": ["breakfast", "lunch", "dinner", "snack"],
            },
            "instructions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Step-by-step cooking instructions",
            },
            "notes": {"type": "string"},
            "foods": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fdc_id": {"type": "integer"},
                        "description": {"type": "string", "description": "USDA food description as returned by search"},
                        "quantity_g": {
                            "type": "number",
                            "description": "Single-serving weight in grams (e.g. 150 for a chicken breast)",
                        },
                        "serving_description": {
                            "type": "string",
                            "description": "Human-readable serving size (e.g. '1 breast', '1 cup')",
                        },
                    },
                    "required": ["fdc_id", "description", "quantity_g"],
                },
            },
        },
        "required": ["name", "meal_type", "instructions", "foods"],
    },
}

_TOOLS = [_SEARCH_TOOL, _FINALIZE_TOOL]

_DESCRIPTION_SYSTEM = """\
You are a recipe analysis assistant with access to the USDA FoodData Central database.

Given a meal description, identify all ingredients, search the USDA database for each one
to get real FDC IDs, suggest realistic single-serving gram quantities, write step-by-step
cooking instructions, and call finalize_recipe with the complete result.

Rules:
- Search for every ingredient before calling finalize_recipe — never invent FDC IDs.
- Use whole foods from USDA Foundation Foods / SR Legacy only.
- Gram quantities should reflect one typical serving (e.g. 150g chicken breast, 80g dry oats).
- Instructions must be detailed enough to actually cook the meal.
"""

_IMAGE_SYSTEM = """\
You are a recipe extraction assistant with access to the USDA FoodData Central database.

Examine the recipe image and extract all ingredients and quantities. Then search the USDA
database for each ingredient to get real FDC IDs, convert any non-gram quantities to grams,
write step-by-step cooking instructions, and call finalize_recipe with the complete result.

Rules:
- Search for every ingredient before calling finalize_recipe — never invent FDC IDs.
- Use whole foods from USDA Foundation Foods / SR Legacy only.
- If an image quantity is given (e.g. "1 cup"), convert it to a reasonable gram weight.
- Instructions must be detailed enough to actually cook the meal.
"""


def _run_agent(messages: list[dict], source: str = "unknown") -> dict:
    """Agentic loop: Claude searches USDA foods then calls finalize_recipe."""
    system = messages[0]["content"] if messages[0]["role"] == "system" else ""
    conv = [m for m in messages if m["role"] != "system"]

    logger.warning("[recipe_ai:%s] starting agent loop model=%s", source, CLAUDE_MODEL_PLANNING)

    for iteration in range(10):
        logger.warning("[recipe_ai:%s] iteration=%d sending %d messages", source, iteration, len(conv))

        response = client.messages.create(
            model=CLAUDE_MODEL_PLANNING,
            max_tokens=4096,
            system=system,
            tools=_TOOLS,
            messages=conv,
        )

        tool_use_blocks = [b for b in response.content if isinstance(b, anthropic.types.ToolUseBlock)]
        text_blocks = [b for b in response.content if isinstance(b, anthropic.types.TextBlock)]

        logger.warning(
            "[recipe_ai:%s] iteration=%d stop_reason=%s tool_blocks=%d text_blocks=%d",
            source, iteration, response.stop_reason, len(tool_use_blocks), len(text_blocks),
        )

        conv.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_use_blocks:
            logger.warning("[recipe_ai:%s] agent stopped without calling finalize_recipe", source)
            raise ValueError("Claude did not produce a recipe. Try a more specific description.")

        tool_results = []
        recipe_output = None

        for block in tool_use_blocks:
            if block.name == "search_foods":
                query = block.input.get("query", "")
                limit = block.input.get("limit", 5)
                logger.warning("[recipe_ai:%s] search_foods query=%r limit=%d", source, query, limit)
                results = _local_search_foods(query, limit)
                logger.warning(
                    "[recipe_ai:%s] search_foods query=%r returned %d results: %s",
                    source, query, len(results),
                    [f"{r.fdc_id} {r.description[:40]}" for r in results],
                )
                result_text = json.dumps([r.model_dump() for r in results])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            elif block.name == "finalize_recipe":
                recipe_output = block.input
                food_summary = [
                    f"{f.get('description','?')[:30]} {f.get('quantity_g','?')}g (fdc={f.get('fdc_id','?')})"
                    for f in recipe_output.get("foods", [])
                ]
                logger.warning(
                    "[recipe_ai:%s] finalize_recipe name=%r meal_type=%r foods=%d: %s",
                    source,
                    recipe_output.get("name"),
                    recipe_output.get("meal_type"),
                    len(recipe_output.get("foods", [])),
                    food_summary,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"status": "ok"}),
                })

        conv.append({"role": "user", "content": tool_results})

        if recipe_output is not None:
            logger.warning("[recipe_ai:%s] agent completed successfully after %d iterations", source, iteration + 1)
            return recipe_output

    raise ValueError("Could not produce a recipe after maximum iterations.")


def analyze_description(description: str) -> dict:
    """Extract a structured recipe from a natural-language meal description."""
    logger.warning("[recipe_ai:description] analyzing description: %r", description[:120])
    messages = [
        {"role": "system", "content": _DESCRIPTION_SYSTEM},
        {"role": "user", "content": f"Please create a recipe from this description: {description}"},
    ]
    return _run_agent(messages, source="description")


def analyze_image(image_base64: str, media_type: str) -> dict:
    """Extract a structured recipe from a base64-encoded recipe image."""
    logger.warning(
        "[recipe_ai:image] analyzing image media_type=%s base64_len=%d",
        media_type, len(image_base64),
    )
    messages = [
        {"role": "system", "content": _IMAGE_SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_base64,
                    },
                },
                {
                    "type": "text",
                    "text": "Please extract the recipe from this image.",
                },
            ],
        },
    ]
    return _run_agent(messages, source="image")
