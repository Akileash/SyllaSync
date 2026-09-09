"""Shared due-date normalization for Sylla Sync."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import LOCAL_TIMEZONE

# Reject day-name-only values like "Wed" or "Friday"
WEEKDAY_ONLY = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)(day)?\.?$",
    re.IGNORECASE,
)

MISSING_DATE_VALUES = {"", "tbd", "no due date", "n/a", "none", "unknown"}

# Timed Calendar events span this many minutes ending at the due time
DEFAULT_EVENT_DURATION_MINUTES = 60


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(LOCAL_TIMEZONE)
    except Exception:  # noqa: BLE001 — fall back if zone DB missing on Windows
        return ZoneInfo("UTC")


def has_explicit_time(value: Any) -> bool:
    """True when the due value includes a non-midnight time component."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    text = str(value).strip()
    if not text or text.lower() in MISSING_DATE_VALUES:
        return False
    # Explicit clock markers or HH:MM patterns
    if re.search(r"\d{1,2}:\d{2}", text) or re.search(
        r"\b(am|pm)\b", text, re.IGNORECASE
    ):
        parsed = pd.to_datetime(text, errors="coerce")
        return bool(pd.notna(parsed))
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return False
    # Date-only ISO "YYYY-MM-DD" or midnight-normalized → treat as all-day
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    return bool(parsed.hour or parsed.minute or parsed.second)


def parse_due_datetime(value: Any) -> datetime | None:
    """Parse due value as timezone-aware local datetime, or None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in MISSING_DATE_VALUES or WEEKDAY_ONLY.match(text):
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    dt = parsed.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    else:
        dt = dt.astimezone(local_tz())
    return dt


def calendar_event_bounds(value: Any) -> dict[str, Any] | None:
    """
    Build Google Calendar start/end dicts.

    Timed dues (e.g. 23:59) → timed event ending at due time.
    Date-only → all-day event (exclusive end = next day).
    """
    dt = parse_due_datetime(value)
    if dt is None:
        return None

    if has_explicit_time(value):
        end = dt
        start = end - timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)
        tz_name = str(local_tz())
        return {
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
            "all_day": False,
            "sort_key": end.isoformat(),
        }

    iso_date = dt.strftime("%Y-%m-%d")
    end_date = (dt.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "start": {"date": iso_date},
        "end": {"date": end_date},
        "all_day": True,
        "sort_key": iso_date,
    }


def due_window_bounds(
    *,
    now: datetime | None = None,
    lookahead_days: int = 7,
) -> tuple[datetime, datetime]:
    """
    Inclusive window from start-of-today through end of day + lookahead.

    Fixes midnight edge cases: date-only items due "today" stay included
    until 23:59:59 local time.
    """
    tz = local_tz()
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    start = datetime.combine(now.date(), time.min, tzinfo=tz)
    end = datetime.combine(
        (now + timedelta(days=lookahead_days)).date(),
        time(23, 59, 59),
        tzinfo=tz,
    )
    return start, end


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
