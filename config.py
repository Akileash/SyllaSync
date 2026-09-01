"""Centralized environment configuration loaded from .env or local.env."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent

# Load .env first, then local.env (local.env overrides if both exist).
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "local.env", override=True)

CANVAS_URL = os.getenv("CANVAS_URL")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
EXCEL_FILE_PATH = os.getenv("EXCEL_FILE_PATH", str(BASE_DIR / "assignments.xlsx"))

REQUIRED_VARS = {
    "CANVAS_URL": CANVAS_URL,
    "CANVAS_TOKEN": CANVAS_TOKEN,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}


def validate_env(require_discord: bool = False, require_google: bool = False) -> None:
    """Raise SystemExit if required environment variables are missing."""
    missing = [name for name, value in REQUIRED_VARS.items() if not value]
    if not require_discord and "DISCORD_WEBHOOK_URL" in missing:
        missing.remove("DISCORD_WEBHOOK_URL")

    if require_google and not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")

    if missing:
        print(
            f"Error: Missing required environment variable(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "Fill in your credentials in local.env (recommended) or .env.",
            file=sys.stderr,
        )
        sys.exit(1)
