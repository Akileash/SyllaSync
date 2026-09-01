"""Shared due-date normalization for Sylla Sync."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

# Reject day-name-only values like "Wed" or "Friday"
WEEKDAY_ONLY = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)(day)?\.?$",
    re.IGNORECASE,
)

MISSING_DATE_VALUES = {"", "tbd", "no due date", "n/a", "none", "unknown"}


def normalize_calendar_date(value: Any) -> str:
    """
    Parse a due date into a canonical calendar date string (YYYY-MM-DD).

    Returns an empty string if the value is missing or unparseable.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    text = str(value).strip()
    if text.lower() in MISSING_DATE_VALUES:
        return ""

    if WEEKDAY_ONLY.match(text):
        return ""

    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""

    return parsed.strftime("%Y-%m-%d")


def format_sheet_date(value: Any) -> str:
    """Format a due date for Google Sheets / Excel (MM/DD/YYYY)."""
    iso_date = normalize_calendar_date(value)
    if not iso_date:
        return ""
    return pd.to_datetime(iso_date).strftime("%m/%d/%Y")


def format_sheet_time(value: Any) -> str:
    """Extract a time string if the due value includes a time component."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    parsed = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(parsed):
        return ""

    if parsed.hour or parsed.minute or parsed.second:
        return parsed.strftime("%I:%M %p").lstrip("0")
    return ""


def format_internal_datetime(value: Any) -> str:
    """Normalize to YYYY-MM-DD or YYYY-MM-DD HH:MM for internal storage."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    text = str(value).strip()
    if text.lower() in MISSING_DATE_VALUES or WEEKDAY_ONLY.match(text):
        return text if text.lower() in MISSING_DATE_VALUES else ""

    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text

    if parsed.hour or parsed.minute or parsed.second:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.strftime("%Y-%m-%d")
