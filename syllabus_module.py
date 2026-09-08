"""Parse syllabus PDFs using deterministic parsers and optional Gemini (google-genai)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from PyPDF2 import PdfReader

from config import GEMINI_API_KEY, GEMINI_MODEL, TERM_YEAR
from course_utils import normalize_course_code
from retry_utils import with_retries

logger = logging.getLogger(__name__)

SYLLABI_DIR = Path(__file__).parent / "syllabi"

MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

SYLLABUS_PROMPT = """You are a syllabus parser. Extract all midterms, finals, and major assignments
from the following syllabus text.

Return ONLY a valid JSON array with no markdown formatting, no code fences, and no extra text.
Each object must have exactly these keys:
- "Course": the course name or code (infer from the syllabus if not explicit)
- "Task": the assignment or exam name
- "Due Date": the due date as a full calendar date in YYYY-MM-DD format (e.g. 2026-09-15). Never use weekday names alone like "Wed" or "Friday". Use "TBD" only if the date is truly unknown.
Skip generic placeholders with no specific item (do not emit bare "Assignments" or "Quizzes" rows).

Example output:
[
  {{"Course": "CS 101", "Task": "Midterm Exam", "Due Date": "2026-03-15"}},
  {{"Course": "CS 101", "Task": "Final Project", "Due Date": "TBD"}}
]

Syllabus text:
{text}
"""


def _extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _normalize_pdf_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _course_from_filename(filename: str) -> str:
    stem = Path(filename).stem.upper().replace("_", "")
    if stem.startswith("MATE") and len(stem) >= 7:
        return f"MAT E {stem[4:7]}"
    normalized = normalize_course_code(stem)
    if normalized:
        return normalized
    match = re.match(r"([A-Z]+)(\d{3,4}[A-Z]?)", stem)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return Path(filename).stem.replace("_", " ")


def _month_day_to_iso(month: str, day: str, year: int | None = None) -> str:
    """Convert month/day to ISO; never emit 'None-MM-DD' when year is missing."""
    resolved_year = year if year is not None else TERM_YEAR
    month_num = MONTH_MAP.get(month.lower()[:4].rstrip("t"), MONTH_MAP.get(month.lower()[:3]))
    if not month_num:
        raise ValueError(f"Unknown month: {month}")
    return f"{resolved_year}-{month_num:02d}-{int(day):02d}"


def _record(course: str, task: str, due_date: str) -> dict[str, Any]:
    return {
        "Source": "Syllabus",
        "Course": course,
        "Task": task,
        "Due Date": due_date,
    }


def parse_weekly_schedule_text(text: str, course: str) -> list[dict[str, Any]]:
    normalized = _normalize_pdf_text(text)
    assignments: list[dict[str, Any]] = []

    for match in re.finditer(
        r"HW(\d+)\s+due\s+5PM\s+Thurs\s+(\w+)\s+(\d+)",
        normalized,
        re.IGNORECASE,
    ):
        hw_num, month, day = match.groups()
        due = _month_day_to_iso(month, day)
        assignments.append(_record(course, f"HW{hw_num}", f"{due} 17:00"))

    for match in re.finditer(
        r"Midterm\s+(\d+)\s+Friday\s+(\w+)\s+(\d+)\s+at\s+5:30pm",
        normalized,
        re.IGNORECASE,
    ):
        exam_num, month, day = match.groups()
        due = _month_day_to_iso(month, day)
        assignments.append(_record(course, f"Midterm {exam_num}", f"{due} 17:30"))

    return assignments


def parse_syllabus_exam_text(text: str, course: str) -> list[dict[str, Any]]:
    """
    Extract exam/assignment entries from a syllabus PDF without an LLM.

    Skips bare category labels (Labs / Assignments / Quizzes / Projects) —
    those are grade-weight headings, not due items. Keeps Midterm/Final when
    a concrete date is present; undated exams become TBD drafts downstream.
    """
    normalized = _normalize_pdf_text(text)
    assignments: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()

    def add(task: str, due: str) -> None:
        key = task.strip().lower()
        if not key or key in seen_tasks:
            return
        # Never emit grade-category placeholders (false positives with nearby dates)
        if key in {
            "lab",
            "labs",
            "assignment",
            "assignments",
            "quiz",
            "quizzes",
            "project",
            "projects",
            "homework",
            "homeworks",
        }:
            return
        seen_tasks.add(key)
        assignments.append(_record(course, task, due))

    for match in re.finditer(
        r"\b(Midterm(?:\s*\d+)?|Final(?:\s*Exam)?)\b"
        r"[^\n]{0,80}?\b(TBD|TBA|\d{4}-\d{2}-\d{2}|"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?)\b",
        normalized,
        re.IGNORECASE,
    ):
        raw_task, raw_due = match.groups()
        task = raw_task.strip()
        if re.fullmatch(r"final", task, re.IGNORECASE):
            task = "Final Exam"

        due_text = raw_due.strip()
        if due_text.upper() in {"TBD", "TBA"}:
            due = "TBD"
        elif re.match(r"\d{4}-\d{2}-\d{2}", due_text):
            due = due_text[:10]
        else:
            parts = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?", due_text)
            if parts:
                month, day, year = parts.groups()
                due = _month_day_to_iso(month, day, int(year) if year else None)
            else:
                due = "TBD"
        add(task, due)

    if re.search(r"final exam", normalized, re.IGNORECASE):
        add("Final Exam", "TBD")
    if re.search(r"\bmidterm\b", normalized, re.IGNORECASE):
        add("Midterm", "TBD")

    deferred = re.search(
        r"deferred final examination is scheduled as follows:\s*Date:\s*(\w+),?\s+(\d+)\s+(\w+),?\s+(\d{4})",
        normalized,
        re.IGNORECASE,
    )
    if deferred:
        _, day, month, year = deferred.groups()
        due = _month_day_to_iso(month, day, int(year))
        add("Deferred Final Exam", due)

    return assignments


def _is_weekly_schedule_pdf(pdf_path: Path) -> bool:
    name = pdf_path.name.lower()
    return "weekly" in name or "schedule" in name


def _is_syllabus_pdf(pdf_path: Path) -> bool:
    name = pdf_path.name.lower()
    return "syllabus" in name and "weekly" not in name and "schedule" not in name


def _parse_with_deterministic_parser(pdf_path: Path, text: str) -> list[dict[str, Any]] | None:
    course = _course_from_filename(pdf_path.name)
    if _is_weekly_schedule_pdf(pdf_path):
        assignments = parse_weekly_schedule_text(text, course)
        if assignments:
            logger.info(
                "Parsed %d assignment(s) from %s using weekly schedule parser.",
                len(assignments),
                pdf_path.name,
            )
            return assignments
        return None
    if _is_syllabus_pdf(pdf_path):
        assignments = parse_syllabus_exam_text(text, course)
        if assignments:
            logger.info(
                "Parsed %d exam(s) from %s using syllabus parser.",
                len(assignments),
                pdf_path.name,
            )
            return assignments
    return None


def _parse_gemini_response(raw_text: str) -> list[dict[str, str]]:
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("Gemini response is not a JSON array")
    results: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "Source": "Syllabus",
                "Course": str(item.get("Course", "Unknown")),
                "Task": str(item.get("Task", "Unknown")),
                "Due Date": str(item.get("Due Date", "TBD")),
            }
        )
    return results


def _build_gemini_client():
    """Return a google-genai Client or None when unavailable/optional."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set — using deterministic parsers only.")
        return None
    try:
        from google import genai  # type: ignore

        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini init failed (%s) — falling back to deterministic parsers.", exc)
        return None


@with_retries(label="gemini.generate")
def _gemini_generate(client: Any, prompt: str) -> str:
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return getattr(response, "text", None) or ""


def _parse_single_pdf(pdf_path: Path, client: Any | None) -> list[dict[str, Any]]:
    logger.info("Parsing syllabus: %s", pdf_path.name)
    text = _extract_pdf_text(pdf_path)
    if not text:
        logger.warning("No text extracted from %s — skipping.", pdf_path.name)
        return []

    deterministic = _parse_with_deterministic_parser(pdf_path, text)
    if deterministic is not None:
        return deterministic

    if not client:
        logger.warning(
            "No Gemini client and no deterministic parser matched %s.",
            pdf_path.name,
        )
        return []

    prompt = SYLLABUS_PROMPT.format(text=text[:30000])
    try:
        raw = _gemini_generate(client, prompt)
    except Exception as exc:  # noqa: BLE001
        logger.error("Gemini parse failed for %s: %s", pdf_path.name, exc)
        return []

    if not raw:
        logger.warning("Empty Gemini response for %s — skipping.", pdf_path.name)
        return []
    return _parse_gemini_response(raw)


def fetch_syllabus_assignments() -> list[dict[str, Any]]:
    """Parse PDFs in syllabi/. Gemini is optional; never aborts the pipeline alone."""
    SYLLABI_DIR.mkdir(exist_ok=True)
    pdf_files = sorted(SYLLABI_DIR.glob("*.pdf"))
    if not pdf_files:
        logger.info("No PDF files found in %s", SYLLABI_DIR)
        return []

    client = _build_gemini_client()
    all_assignments: list[dict[str, Any]] = []
    for pdf_path in pdf_files:
        try:
            assignments = _parse_single_pdf(pdf_path, client)
            all_assignments.extend(assignments)
            logger.info("Parsed %d assignment(s) from %s.", len(assignments), pdf_path.name)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON from Gemini for %s: %s", pdf_path.name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to parse %s: %s", pdf_path.name, exc)

    logger.info("Total syllabus assignments parsed: %d", len(all_assignments))
    return all_assignments
