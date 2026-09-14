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
    """
    Normalize to YYYY-MM-DD or YYYY-MM-DD HH:MM in LOCAL_TIMEZONE.

    Edge case: Canvas returns UTC (`…Z`). Convert to local before stripping
    tzinfo so 05:59Z on the 17th becomes 23:59 on the 16th in Edmonton —
    otherwise Calendar creates a second timed event on the wrong day.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    text = str(value).strip()
    if text.lower() in MISSING_DATE_VALUES or WEEKDAY_ONLY.match(text):
        return text if text.lower() in MISSING_DATE_VALUES else ""

    # Prefer timezone-aware path for Canvas ISO strings
    try:
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        if "T" in iso or "+" in iso[-6:] or iso.count("-") >= 3:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                # Naive ISO without offset — treat as already local
                pass
            else:
                dt = dt.astimezone(local_tz()).replace(tzinfo=None)
            if dt.hour or dt.minute or dt.second:
                return dt.strftime("%Y-%m-%d %H:%M")
            return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass

    parsed = pd.to_datetime(text, errors="coerce", utc=False)
    if pd.isna(parsed):
        return text

    # If pandas attached tz, convert to local
    if getattr(parsed, "tzinfo", None) is not None or (
        hasattr(parsed, "tz") and parsed.tz is not None
    ):
        try:
            parsed = parsed.tz_convert(str(local_tz())).tz_localize(None)
        except (TypeError, AttributeError, ValueError):
            pass

    if parsed.hour or parsed.minute or parsed.second:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.strftime("%Y-%m-%d")


# Pull "Due date …" / "due …" clauses out of Canvas/syllabus titles
# (may appear on the same line or after a newline).
_EMBEDDED_DUE_RE = re.compile(
    r"""
    [\s\-–—:,]*                 # separators before the due clause
    (?:due\s*date|due\s*by|due)\s*[:\-]?\s*
    (?:
        (?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+  # optional weekday
    )?
    (
        # Month Day[, Year][, time]
        (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*
        \s+\d{1,2}
        (?:st|nd|rd|th)?
        (?:,?\s*\d{4})?
        (?:
            \s*,?\s*(?:at\s+)?\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?
        )?
        |
        # ISO date[, time]
        \d{4}-\d{2}-\d{2}
        (?:\s+\d{1,2}:\d{2}(?::\d{2})?)?
        |
        # Numeric M/D[/YYYY][, time]
        \d{1,2}/\d{1,2}(?:/\d{2,4})?
        (?:
            \s*,?\s*(?:at\s+)?\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?
        )?
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


def split_title_and_due(title: Any, existing_due: Any = "") -> tuple[str, str]:
    """
    Always separate assignment name from embedded due text.

    Call this at every ingest/write boundary so titles never keep
    "Due date Oct 6, 11:45 PM" in the ASSIGNMENT column.

    Example:
      "Online Assignment 2- Due date Oct 6, 11:45 PM"
        → ("Online Assignment 2", "2026-10-06 23:45")
    """
    from config import TERM_YEAR  # local import avoids circular load at import time

    raw = str(title or "").strip()
    # Normalize newlines so "Title\\nDue date …" still matches
    raw_flat = re.sub(r"[\r\n]+", " ", raw)
    raw_flat = re.sub(r"\s+", " ", raw_flat).strip()

    existing = ""
    existing_raw = str(existing_due or "").strip()
    if existing_raw and existing_raw.lower() not in MISSING_DATE_VALUES:
        existing = format_internal_datetime(existing_raw) or existing_raw

    if not raw_flat:
        return "", existing

    match = _EMBEDDED_DUE_RE.search(raw_flat)
    if not match:
        return re.sub(r"[\s\-–—]+$", "", raw_flat).strip() or raw_flat, existing

    due_fragment = match.group(1).strip()
    clean_title = raw_flat[: match.start()].strip(" -\u2013\u2014:,\t")
    clean_title = re.sub(r"[\s\-–—]+$", "", clean_title).strip() or raw_flat

    fragment = due_fragment
    if not re.search(r"\d{4}", fragment) and not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", fragment):
        fragment = re.sub(
            r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
            rf"\1 \2, {TERM_YEAR}",
            fragment,
            count=1,
        )

    parsed_due = format_internal_datetime(fragment) or format_internal_datetime(due_fragment)

    # Prefer a real API/structured due when present; still always return clean_title
    if existing and normalize_calendar_date(existing):
        return clean_title, existing
    if parsed_due and normalize_calendar_date(parsed_due):
        return clean_title, parsed_due
    return clean_title, existing
