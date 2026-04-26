import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/app",
)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_IOS_CLIENT_ID: str = os.environ.get("GOOGLE_IOS_CLIENT_ID", "")
USDA_API_KEY: str = os.environ.get("USDA_API_KEY", "DEMO_KEY")

USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Only use Foundation Foods and SR Legacy — whole foods, no branded products
USDA_DATA_TYPES = ["Foundation", "SR Legacy"]

# Default model for all Claude tasks — override per-task below
CLAUDE_MODEL_DEFAULT = os.environ.get("CLAUDE_MODEL_DEFAULT", "claude-sonnet-4-6")

# Task-specific model overrides
# Sonnet for complex reasoning: meal plan generation, multi-turn tool-use editing
CLAUDE_MODEL_PLANNING = os.environ.get("CLAUDE_MODEL_PLANNING", CLAUDE_MODEL_DEFAULT)
CLAUDE_MODEL_EDITING = os.environ.get("CLAUDE_MODEL_EDITING", CLAUDE_MODEL_DEFAULT)
# Haiku for lightweight tasks: title generation, preference extraction
CLAUDE_MODEL_TITLE = os.environ.get("CLAUDE_MODEL_TITLE", "claude-haiku-4-5-20251001")
CLAUDE_MODEL_EXTRACTION = os.environ.get("CLAUDE_MODEL_EXTRACTION", "claude-haiku-4-5-20251001")

DEV_MODE: bool = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")
