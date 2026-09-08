"""Fetch assignments from the Canvas LMS API (with past-due lookback)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import (
    ALLOWED_COURSES,
    CANVAS_PAST_DUE_DAYS,
    CANVAS_TOKEN,
    CANVAS_URL,
)
from course_utils import course_matches_allowed, normalize_course_code, parse_allowed_courses
from date_utils import format_internal_datetime
from retry_utils import with_retries
from schedule_module import load_schedule

logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = ("Source", "Course", "Task", "Due Date")


class CanvasFetchError(RuntimeError):
    """Raised when Canvas cannot be reached or credentials are invalid."""


def _format_due_date(due_at: str | None) -> str | None:
    if not due_at:
        return None
    iso_input = due_at.replace("Z", "+00:00") if "Z" in str(due_at) else due_at
    formatted = format_internal_datetime(iso_input)
    if formatted:
        return formatted
    try:
        dt = datetime.fromisoformat(iso_input)
        return (
            dt.strftime("%Y-%m-%d %H:%M")
            if (dt.hour or dt.minute)
            else dt.strftime("%Y-%m-%d")
        )
    except (ValueError, TypeError):
        logger.warning("Could not parse due date: %s", due_at)
        return due_at


def _within_window(due_at: str | None, lookback_days: int) -> bool:
    """
    Keep assignments with no due date, future dues, or dues within lookback.

    Edge case: past-due items still refresh for late submissions / status changes.
    """
    if not due_at:
        return True
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        return due >= cutoff
    except (ValueError, TypeError):
        return True


def section_allowed(course_name: str, schedule: dict[str, Any]) -> bool:
    """
    If schedule.json lists sections for this course, require Canvas title to
    mention at least one of those section codes (e.g. LEC A1, LAB D26).
    """
    sections = schedule.get("sections") or []
    if not sections:
        return True
    course_key = (normalize_course_code(course_name) or course_name).upper()
    mine = [
        s
        for s in sections
        if (normalize_course_code(str(s.get("course") or "")) or str(s.get("course") or "")).upper()
        == course_key
    ]
    if not mine:
        # Course not in schedule → allow (schedule incomplete shouldn't block)
        return True
    name_u = course_name.upper()
    for s in mine:
        stype = str(s.get("type") or "").upper()
        code = str(s.get("section") or "").upper()
        if code and code in name_u:
            return True
        if stype and code and f"{stype} {code}" in name_u:
            return True
    # Canvas parent course shells often omit section letters — still allow
    if "LEC" not in name_u and "LAB" not in name_u and "SEM" not in name_u:
        return True
    return False


@with_retries(label="canvas.get_courses")
def _list_courses(canvas: Canvas):
    return list(canvas.get_courses(enrollment_state="active"))


@with_retries(label="canvas.get_assignments")
def _list_assignments(course):
    return list(course.get_assignments(order_by="due_at"))


def fetch_canvas_assignments(
    *,
    lookback_days: int | None = None,
    enforce_schedule: bool = True,
) -> list[dict[str, Any]]:
    """
    Fetch Canvas assignments for allowed courses.

    Raises:
        CanvasFetchError: on auth / connectivity / 5xx failures (fail loudly).
    """
    if not CANVAS_URL or not CANVAS_TOKEN:
        raise CanvasFetchError("CANVAS_URL and CANVAS_TOKEN must be set in .env")

    lookback = CANVAS_PAST_DUE_DAYS if lookback_days is None else lookback_days
    assignments: list[dict[str, Any]] = []
    allowed_courses = parse_allowed_courses(ALLOWED_COURSES)
    schedule = load_schedule() if enforce_schedule else {"sections": []}

    try:
        canvas = Canvas(CANVAS_URL, CANVAS_TOKEN)
        courses = _list_courses(canvas)
    except CanvasException as exc:
        raise CanvasFetchError(f"Failed to connect to Canvas: {exc}") from exc
    except Exception as exc:  # network / unexpected
        raise CanvasFetchError(f"Canvas request failed: {exc}") from exc

    for course in courses:
        raw_name = (
            getattr(course, "course_code", None)
            or getattr(course, "name", None)
            or f"Course {course.id}"
        )

        if not course_matches_allowed(raw_name, allowed_courses):
            continue
        if enforce_schedule and not section_allowed(raw_name, schedule):
            logger.info("Skipping Canvas course (section filter): %s", raw_name)
            continue

        course_name = normalize_course_code(raw_name) or raw_name

        try:
            course_assignments = _list_assignments(course)
        except CanvasException as exc:
            logger.warning("Skipping course '%s': %s", course_name, exc)
            continue

        for assignment in course_assignments:
            if getattr(assignment, "published", True) is False:
                continue
            due_at = getattr(assignment, "due_at", None)
            if not _within_window(due_at, lookback):
                continue
            assignments.append(
                {
                    "Source": "Canvas",
                    "Course": course_name,
                    "Task": getattr(assignment, "name", "Untitled Assignment"),
                    "Due Date": _format_due_date(due_at) or "No due date",
                    "Canvas ID": getattr(assignment, "id", None),
                    "assignment_id": getattr(assignment, "id", None),
                }
            )

    logger.info("Fetched %d Canvas assignment(s) (lookback=%dd).", len(assignments), lookback)
    return assignments
