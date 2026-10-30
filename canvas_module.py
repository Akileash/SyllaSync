"""Fetch assignments from the Canvas LMS API (with past-due lookback)."""

from __future__ import annotations

import logging
from safe_text import redact_text
from datetime import datetime, timedelta, timezone
from typing import Any

from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import (
    ALLOWED_COURSES,
    CANVAS_PAST_DUE_DAYS,
    CANVAS_READ_ANNOUNCEMENTS,
    CANVAS_TOKEN,
    CANVAS_URL,
    TERM_YEAR,
)
from course_utils import course_matches_allowed, normalize_course_code, parse_allowed_courses
from date_utils import (
    apply_lab_monday_due,
    format_internal_datetime,
    normalize_calendar_date,
    split_title_and_due,
)
from retry_utils import with_retries
from schedule_module import load_schedule

logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = ("Source", "Course", "Task", "Due Date")

# Canvas assignments whose due_at falls in a year before the term year (course
# shells copied from last year). Filled by fetch_canvas_assignments so callers
# can surface them; the dates are never rewritten to the current year.
STALE_YEAR_WARNINGS: list[dict[str, Any]] = []


class CanvasFetchError(RuntimeError):
    """Raised when Canvas cannot be reached or credentials are invalid."""



def explain_canvas_error(exc: BaseException) -> str:
    """Actionable Canvas failure text. Never includes the token."""
    text = redact_text(exc).lower()
    if any(marker in text for marker in ("401", "invalid access token", "unauthorized")):
        return (
            "Canvas rejected CANVAS_TOKEN (unauthorized). The token is missing, "
            "expired, or revoked. In Canvas open Account → Settings → New Access Token, "
            "then put the new token in .env or local.env. The token was not logged."
        )
    if "403" in text or "forbidden" in text:
        return (
            "Canvas refused this request (forbidden). Confirm the token belongs to "
            "the account that is enrolled in the courses, and that CANVAS_URL is "
            "your school's Canvas host. The token was not logged."
        )
    if any(marker in text for marker in ("timeout", "timed out", "connection", "503", "502")):
        return (
            "Canvas could not be reached (network or temporary outage). "
            f"Details: {redact_text(exc)} "
            "Check your connection and re-run. Nothing was written."
        )
    return (
        "Canvas request failed. Check CANVAS_URL and CANVAS_TOKEN in .env or local.env. "
        f"Details: {redact_text(exc)}"
    )



def _format_due_date(due_at: str | None) -> str | None:
    """
    Convert Canvas due_at (usually UTC) into local YYYY-MM-DD [HH:MM].

    Must convert timezone before storing — otherwise Calendar shows wrong-day
    timed events (e.g. 05:59Z → 4:59–5:59am) alongside an older all-day event.
    """
    if not due_at:
        return None
    formatted = format_internal_datetime(due_at)
    if formatted:
        return formatted
    logger.warning("Could not parse due date: %s", due_at)
    return str(due_at)


def _has_due_override(course_name: str, raw_title: str) -> bool:
    """True when due_overrides.json pins this item, so the lookback window must not drop it."""
    from dedupe_module import fuzzy_match_key
    from sheets_module import load_due_overrides

    title, _ = split_title_and_due(raw_title, "")
    return fuzzy_match_key(course_name, title) in load_due_overrides()


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


@with_retries(label="canvas.get_announcements")
def _list_announcements(course):
    return list(course.get_discussion_topics(only_announcements=True))


def is_stale_year_due(due_formatted: Any, term_year: int | None = None) -> bool:
    """True when a (local) due date is in a calendar year before the term year."""
    iso = normalize_calendar_date(due_formatted)
    if not iso:
        return False
    return int(iso[:4]) < int(term_year if term_year is not None else TERM_YEAR)


def _course_label(course) -> str:
    return (
        getattr(course, "course_code", None)
        or getattr(course, "name", None)
        or f"Course {course.id}"
    )


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

    STALE_YEAR_WARNINGS.clear()

    for course in courses:
        raw_name = _course_label(course)

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
            if due_at and is_stale_year_due(_format_due_date(due_at)):
                # Surface, do not silently drop or auto-convert to this year.
                logger.warning(
                    "stale-year due date: %s '%s' (Canvas id %s) due_at=%s is before "
                    "term year %d - not converted; check Canvas/announcements for the "
                    "current date.",
                    course_name,
                    getattr(assignment, "name", "?"),
                    getattr(assignment, "id", "?"),
                    due_at,
                    TERM_YEAR,
                )
                STALE_YEAR_WARNINGS.append(
                    {
                        "Course": course_name,
                        "Task": getattr(assignment, "name", ""),
                        "Canvas ID": getattr(assignment, "id", None),
                        "due_at": due_at,
                        "term_year": TERM_YEAR,
                    }
                )
            if not _within_window(due_at, lookback) and not _has_due_override(
                course_name, getattr(assignment, "name", "")
            ):
                continue
            raw_title = getattr(assignment, "name", "Untitled Assignment")
            due_formatted = _format_due_date(due_at) or "No due date"
            # Strip "Due date …" out of Canvas names so Sheets titles stay clean
            clean_title, due_formatted = split_title_and_due(raw_title, due_formatted)
            due_formatted = apply_lab_monday_due(
                course_name, clean_title, due_formatted
            )
            assignments.append(
                {
                    "Source": "Canvas",
                    "Course": course_name,
                    "Task": clean_title,
                    "Due Date": due_formatted,
                    "Canvas ID": getattr(assignment, "id", None),
                    "assignment_id": getattr(assignment, "id", None),
                }
            )

    logger.info("Fetched %d Canvas assignment(s) (lookback=%dd).", len(assignments), lookback)
    if STALE_YEAR_WARNINGS:
        logger.warning(
            "%d Canvas assignment(s) have stale-year due dates (see warnings above).",
            len(STALE_YEAR_WARNINGS),
        )
    return assignments


def _posted_year_month(value: Any) -> tuple[int | None, int | None]:
    local = format_internal_datetime(value) if value else ""
    iso = normalize_calendar_date(local)
    if not iso:
        return None, None
    return int(iso[:4]), int(iso[5:7])


def fetch_canvas_announcement_exams(
    *,
    enforce_schedule: bool = True,
    canvas: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Read Canvas announcements (read-only) and extract dated exam rows.

    Rows look like syllabus rows (Course / Task / Due Date) so the dedupe
    engine can merge them with an undated syllabus "Midterm" and upgrade it.
    Never raises: any Canvas or parsing error is logged and yields [].
    """
    if not CANVAS_READ_ANNOUNCEMENTS:
        logger.info("Canvas announcement scan disabled (CANVAS_READ_ANNOUNCEMENTS=0).")
        return []
    if canvas is None and (not CANVAS_URL or not CANVAS_TOKEN):
        logger.warning("Canvas announcement scan skipped: CANVAS_URL/CANVAS_TOKEN not set.")
        return []

    from syllabus_module import html_to_text, parse_announcement_exam_text

    rows: list[dict[str, Any]] = []
    try:
        allowed_courses = parse_allowed_courses(ALLOWED_COURSES)
        schedule = load_schedule() if enforce_schedule else {"sections": []}
        client = canvas if canvas is not None else Canvas(CANVAS_URL, CANVAS_TOKEN)
        courses = _list_courses(client)
    except Exception as exc:  # noqa: BLE001 - announcements are best-effort
        logger.warning("Canvas announcement scan skipped: %s", exc)
        return []

    for course in courses:
        try:
            raw_name = _course_label(course)
            if not course_matches_allowed(raw_name, allowed_courses):
                continue
            if enforce_schedule and not section_allowed(raw_name, schedule):
                continue
            course_name = normalize_course_code(raw_name) or raw_name
            topics = _list_announcements(course)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Announcements unavailable for %s: %s",
                getattr(course, "id", "?"),
                exc,
            )
            continue

        # Newest first so a rescheduled date wins over an older announcement.
        topics = sorted(
            topics,
            key=lambda t: str(getattr(t, "posted_at", "") or ""),
            reverse=True,
        )
        for topic in topics:
            try:
                title = str(getattr(topic, "title", "") or "")
                body = html_to_text(getattr(topic, "message", "") or "")
                posted_at = getattr(topic, "posted_at", None)
                year, month = _posted_year_month(posted_at)
                found = parse_announcement_exam_text(
                    f"{title}\n{body}",
                    course_name,
                    base_year=year,
                    posted_month=month,
                    source_ref=(
                        f"Canvas announcement {getattr(topic, 'id', '?')} "
                        f"'{title}' ({posted_at})"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not parse announcement %s in %s: %s",
                    getattr(topic, "id", "?"),
                    course_name,
                    exc,
                )
                continue
            for row in found:
                if is_stale_year_due(row.get("Due Date")):
                    logger.warning(
                        "stale-year due date: announcement %s gives %s %s on %s "
                        "(before term year %d) - ignored, not converted.",
                        row.get("Origin"),
                        row.get("Course"),
                        row.get("Task"),
                        row.get("Due Date"),
                        TERM_YEAR,
                    )
                    continue
                logger.info(
                    "Announcement exam date: %s %s -> %s (%s)",
                    row.get("Course"),
                    row.get("Task"),
                    row.get("Due Date"),
                    row.get("Origin"),
                )
                rows.append(row)

    logger.info("Extracted %d exam date(s) from Canvas announcements.", len(rows))
    return rows
