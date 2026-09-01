"""Fetch upcoming assignments from the Canvas LMS API."""

import logging
from datetime import datetime, timezone
from typing import Any

from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import CANVAS_TOKEN, CANVAS_URL
from date_utils import format_internal_datetime

logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = ("Source", "Course", "Task", "Due Date")


def _format_due_date(due_at: str | None) -> str | None:
    """Convert Canvas ISO timestamp to a calendar date (and time if present)."""
    if not due_at:
        return None

    iso_input = due_at.replace("Z", "+00:00") if "Z" in str(due_at) else due_at
    formatted = format_internal_datetime(iso_input)
    if formatted:
        return formatted

    try:
        dt = datetime.fromisoformat(iso_input)
        return dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute) else dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        logger.warning("Could not parse due date: %s", due_at)
        return due_at


def _is_upcoming(due_at: str | None) -> bool:
    """Return True if the assignment is due in the future or has no due date."""
    if not due_at:
        return True

    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        return due >= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def fetch_canvas_assignments() -> list[dict[str, Any]]:
    """
    Connect to Canvas and fetch upcoming assignments for all active courses.

    Returns:
        List of dicts with keys: Source, Course, Task, Due Date.
    """
    if not CANVAS_URL or not CANVAS_TOKEN:
        raise ValueError("CANVAS_URL and CANVAS_TOKEN must be set in .env")

    assignments: list[dict[str, Any]] = []

    try:
        canvas = Canvas(CANVAS_URL, CANVAS_TOKEN)
        courses = canvas.get_courses(enrollment_state="active")
    except CanvasException as exc:
        logger.error("Failed to connect to Canvas: %s", exc)
        raise

    for course in courses:
        # Prefer course code (e.g. "ECE 210") for cleaner matching in the tracker.
        course_name = (
            getattr(course, "course_code", None)
            or getattr(course, "name", None)
            or f"Course {course.id}"
        )

        try:
            course_assignments = course.get_assignments(
                order_by="due_at",
                bucket="upcoming",
            )
        except CanvasException as exc:
            logger.warning("Skipping course '%s': %s", course_name, exc)
            continue

        for assignment in course_assignments:
            due_at = getattr(assignment, "due_at", None)

            if not _is_upcoming(due_at):
                continue

            assignments.append(
                {
                    "Source": "Canvas",
                    "Course": course_name,
                    "Task": getattr(assignment, "name", "Untitled Assignment"),
                    "Due Date": _format_due_date(due_at) or "No due date",
                }
            )

    logger.info("Fetched %d upcoming Canvas assignment(s).", len(assignments))
    return assignments
