"""Download syllabus and schedule PDFs from Canvas course modules."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import requests
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import ALLOWED_COURSES, CANVAS_TOKEN, CANVAS_URL
from course_utils import compact_course_code, course_matches_allowed, parse_allowed_courses

logger = logging.getLogger(__name__)

SYLLABI_DIR = Path(__file__).parent / "syllabi"
FILE_TITLE_PATTERNS = re.compile(
    r"syllabus|weekly\s*schedule|tentative\s*weekly\s*schedule",
    re.IGNORECASE,
)


def _safe_filename(course_code: str, title: str) -> str:
    """Build a stable PDF filename for a module file."""
    title_lower = title.lower()
    if "weekly" in title_lower or "schedule" in title_lower:
        kind = "Weekly_Schedule"
    else:
        kind = "Syllabus"
    return f"{course_code}_{kind}.pdf"


def _download_file(url: str, dest: Path, token: str) -> None:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    response.raise_for_status()
    dest.write_bytes(response.content)


def download_syllabi_from_canvas(
    course_filter: str | None = None,
    allowed_courses: list[str] | None = None,
    dest_dir: Path | None = None,
) -> list[Path]:
    """
    Download syllabus/schedule PDFs from Canvas module items.

    Args:
        course_filter: Optional single-course substring filter (legacy CLI flag).
        allowed_courses: Optional list of canonical course codes to include.
        dest_dir: Output directory (defaults to syllabi/).

    Returns:
        List of downloaded file paths.
    """
    if not CANVAS_URL or not CANVAS_TOKEN:
        raise ValueError("CANVAS_URL and CANVAS_TOKEN must be set in .env")

    output_dir = dest_dir or SYLLABI_DIR
    output_dir.mkdir(exist_ok=True)

    if allowed_courses is None:
        allowed_courses = parse_allowed_courses(ALLOWED_COURSES)

    canvas = Canvas(CANVAS_URL, CANVAS_TOKEN)
    downloaded: list[Path] = []

    try:
        courses = canvas.get_courses(enrollment_state="active")
    except CanvasException as exc:
        logger.error("Failed to connect to Canvas: %s", exc)
        raise

    for course in courses:
        course_name = (
            getattr(course, "course_code", None)
            or getattr(course, "name", None)
            or f"Course {course.id}"
        )

        if course_filter and course_filter.lower() not in course_name.lower():
            continue

        if allowed_courses and not course_matches_allowed(course_name, allowed_courses):
            continue

        course_code = compact_course_code(course_name)
        logger.info("Scanning Canvas modules for %s (%s)", course_name, course_code)

        try:
            modules = course.get_modules()
        except CanvasException as exc:
            logger.warning("Could not list modules for '%s': %s", course_name, exc)
            continue

        for module in modules:
            try:
                items = module.get_module_items()
            except CanvasException as exc:
                logger.warning(
                    "Could not list items in module '%s': %s", module.name, exc
                )
                continue

            for item in items:
                if getattr(item, "type", None) != "File":
                    continue

                title = getattr(item, "title", "") or ""
                if not FILE_TITLE_PATTERNS.search(title):
                    continue

                content_id = getattr(item, "content_id", None)
                if not content_id:
                    continue

                dest = output_dir / _safe_filename(course_code, title)
                try:
                    file_obj = canvas.get_file(content_id)
                    _download_file(file_obj.url, dest, CANVAS_TOKEN)
                    downloaded.append(dest)
                    logger.info("Downloaded %s from Canvas", dest.name)
                except Exception as exc:
                    logger.error("Failed to download '%s': %s", title, exc)

    return downloaded
