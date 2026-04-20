import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/app",
)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
USDA_API_KEY: str = os.environ.get("USDA_API_KEY", "DEMO_KEY")

USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Only use Foundation Foods and SR Legacy — whole foods, no branded products
USDA_DATA_TYPES = ["Foundation", "SR Legacy"]

CLAUDE_MODEL = "claude-sonnet-4-6"
