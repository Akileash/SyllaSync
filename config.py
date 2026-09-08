"""Centralized environment configuration loaded from .env or local.env."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "local.env", override=True)

CANVAS_URL = os.getenv("CANVAS_URL")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
EXCEL_FILE_PATH = os.getenv("EXCEL_FILE_PATH", str(BASE_DIR / "assignments.xlsx"))
ALLOWED_COURSES = os.getenv("ALLOWED_COURSES", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
SCHEDULE_PATH = BASE_DIR / "schedule.json"

# Optional Gemini model override (google-genai SDK)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Past-due Canvas lookback window (days)
CANVAS_PAST_DUE_DAYS = int(os.getenv("CANVAS_PAST_DUE_DAYS", "21"))

# Local timezone for Calendar timed events / Discord windows
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "America/Edmonton")

# Dynamic academic term year (override with TERM_YEAR=2026)
def infer_term_year(today: date | None = None) -> int:
    """
    Infer academic term year from the execution date.

    Fall (Aug–Dec) → current calendar year.
    Winter/Spring (Jan–Jul) → previous calendar year as the Fall-start year
    for syllabi that omit the year on autumn dates; winter months use current year.
    """
    today = today or date.today()
    override = os.getenv("TERM_YEAR")
    if override and override.isdigit():
        return int(override)
    return today.year


TERM_YEAR = infer_term_year()

REQUIRED_ALWAYS = {
    "CANVAS_URL": CANVAS_URL,
    "CANVAS_TOKEN": CANVAS_TOKEN,
}


def validate_env(
    require_discord: bool = False,
    require_google: bool = False,
    require_calendar: bool = False,
    require_gemini: bool = False,
) -> None:
    """Raise SystemExit if required environment variables are missing."""
    missing = [name for name, value in REQUIRED_ALWAYS.items() if not value]

    if require_discord and not DISCORD_WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")
    if require_google and not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")
    if require_calendar and not GOOGLE_CALENDAR_ID:
        missing.append("GOOGLE_CALENDAR_ID")
    if require_gemini and not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

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
