"""Download syllabus and schedule PDFs from Canvas (Modules, Files, Syllabus page)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from canvasapi import Canvas
from canvasapi.exceptions import CanvasException

from config import ALLOWED_COURSES, CANVAS_TOKEN, CANVAS_URL
from course_utils import compact_course_code, course_matches_allowed, parse_allowed_courses
from retry_utils import with_retries
from schedule_module import load_schedule

logger = logging.getLogger(__name__)

SYLLABI_DIR = Path(__file__).parent / "syllabi"
FILE_TITLE_PATTERNS = re.compile(
    r"syllabus|weekly\s*schedule|tentative\s*weekly\s*schedule|course\s*outline",
    re.IGNORECASE,
)
PDF_HREF_RE = re.compile(
    r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
    re.IGNORECASE,
)


def _safe_filename(course_code: str, title: str, suffix: str = "") -> str:
    title_lower = title.lower()
    if "weekly" in title_lower or "schedule" in title_lower:
        kind = "Weekly_Schedule"
    else:
        kind = "Syllabus"
    extra = f"_{suffix}" if suffix else ""
    return f"{course_code}_{kind}{extra}.pdf"


@with_retries(label="canvas.download_file")
def _download_file(url: str, dest: Path, token: str) -> None:
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").lower()
    # Edge case: Canvas sometimes returns HTML error pages with 200
    if "html" in content_type and not dest.name.lower().endswith(".html"):
        raise ValueError(f"Expected PDF but got Content-Type={content_type}")
    if response.content[:4] != b"%PDF" and "pdf" in dest.name.lower():
        # Some valid PDFs may not start instantly after redirects; warn soft
        if b"%PDF" not in response.content[:2048]:
            logger.warning("Downloaded content for %s may not be a PDF", dest.name)
    dest.write_bytes(response.content)


def _section_ok(course_name: str) -> bool:
    from canvas_module import section_allowed

    return section_allowed(course_name, load_schedule())


def download_syllabi_from_canvas(
    course_filter: str | None = None,
    allowed_courses: list[str] | None = None,
    dest_dir: Path | None = None,
) -> list[Path]:
    """
    Download syllabus/schedule PDFs from:
      1) Course Modules (File items)
      2) Course Files browser
      3) Syllabus page HTML body (embedded PDF links)
    """
    if not CANVAS_URL or not CANVAS_TOKEN:
        raise ValueError("CANVAS_URL and CANVAS_TOKEN must be set in .env")

    output_dir = dest_dir or SYLLABI_DIR
    output_dir.mkdir(exist_ok=True)

    if allowed_courses is None:
        allowed_courses = parse_allowed_courses(ALLOWED_COURSES)

    canvas = Canvas(CANVAS_URL, CANVAS_TOKEN)
    downloaded: list[Path] = []
    seen_urls: set[str] = set()

    try:
        courses = list(canvas.get_courses(enrollment_state="active"))
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
        if not _section_ok(course_name):
            logger.info("Skipping syllabus download (section filter): %s", course_name)
            continue

        course_code = compact_course_code(course_name)
        logger.info("Scanning Canvas for syllabi: %s (%s)", course_name, course_code)

        # --- 1) Modules ---
        try:
            for module in course.get_modules():
                try:
                    items = module.get_module_items()
                except CanvasException:
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
                        url = file_obj.url
                        if url in seen_urls:
                            continue
                        _download_file(url, dest, CANVAS_TOKEN)
                        seen_urls.add(url)
                        downloaded.append(dest)
                        logger.info("Downloaded %s from Modules", dest.name)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed to download module file '%s': %s", title, exc)
        except CanvasException as exc:
            logger.warning("Could not list modules for '%s': %s", course_name, exc)

        # --- 2) Course Files ---
        try:
            for file_obj in course.get_files():
                display = getattr(file_obj, "display_name", "") or getattr(file_obj, "filename", "") or ""
                if not FILE_TITLE_PATTERNS.search(display):
                    continue
                if not str(display).lower().endswith(".pdf"):
                    continue
                url = getattr(file_obj, "url", None)
                if not url or url in seen_urls:
                    continue
                dest = output_dir / _safe_filename(course_code, display, suffix="Files")
                try:
                    _download_file(url, dest, CANVAS_TOKEN)
                    seen_urls.add(url)
                    downloaded.append(dest)
                    logger.info("Downloaded %s from Files", dest.name)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to download file '%s': %s", display, exc)
        except CanvasException as exc:
            logger.debug("Files API unavailable for '%s': %s", course_name, exc)

        # --- 3) Syllabus page HTML ---
        try:
            # canvasapi Course has syllabus_body attribute when include[]=syllabus_body
            detailed = canvas.get_course(course.id, include=["syllabus_body"])
            body = getattr(detailed, "syllabus_body", None) or ""
            if body:
                for href in PDF_HREF_RE.findall(body):
                    url = href if href.startswith("http") else urljoin(CANVAS_URL, href)
                    if url in seen_urls:
                        continue
                    dest = output_dir / _safe_filename(course_code, "Syllabus", suffix="Page")
                    try:
                        _download_file(url, dest, CANVAS_TOKEN)
                        seen_urls.add(url)
                        downloaded.append(dest)
                        logger.info("Downloaded %s from Syllabus page", dest.name)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed Syllabus-page PDF '%s': %s", url, exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Syllabus page scrape skipped for '%s': %s", course_name, exc)

    return downloaded
