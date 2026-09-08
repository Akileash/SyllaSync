"""Load the local weekly class/lab schedule (schedule.json)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import BASE_DIR

logger = logging.getLogger(__name__)

SCHEDULE_PATH = BASE_DIR / "schedule.json"


@lru_cache(maxsize=1)
def load_schedule() -> dict[str, Any]:
    """
    Load schedule.json if present.

    Returns an empty structure when the file is missing so callers can
    keep working without a timetable on disk.
    """
    if not SCHEDULE_PATH.exists():
        logger.info("No schedule.json found — section filtering unavailable.")
        return {"term": "", "sections": []}

    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read schedule.json: %s", exc)
        return {"term": "", "sections": []}

    sections = data.get("sections") or []
    logger.info(
        "Loaded schedule.json (%s) with %d section(s).",
        data.get("term") or "unknown term",
        len(sections),
    )
    return data


def enrolled_courses() -> list[str]:
    """Unique course codes from the schedule (e.g. ['MAT E 201', 'MATH 201'])."""
    courses: list[str] = []
    seen: set[str] = set()
    for section in load_schedule().get("sections", []):
        course = str(section.get("course") or "").strip()
        if course and course not in seen:
            seen.add(course)
            courses.append(course)
    return courses


def sections_for_course(course: str) -> list[dict[str, Any]]:
    """Return all LEC/LAB/SEM rows for a course code."""
    course_upper = course.strip().upper()
    return [
        s
        for s in load_schedule().get("sections", [])
        if str(s.get("course") or "").strip().upper() == course_upper
    ]


def lab_sections() -> list[dict[str, Any]]:
    """Return only LAB rows from the schedule."""
    return [
        s
        for s in load_schedule().get("sections", [])
        if str(s.get("type") or "").upper() == "LAB"
    ]


def section_label(section: dict[str, Any]) -> str:
    """Pretty label like 'MATH 209 LAB EL05'."""
    course = section.get("course", "")
    stype = section.get("type", "")
    code = section.get("section", "")
    return f"{course} {stype} {code}".strip()
