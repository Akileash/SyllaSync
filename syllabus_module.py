"""Parse syllabus PDFs using deterministic parsers and the Gemini API."""

import json
import logging
import re
from pathlib import Path
from typing import Any

import google.generativeai as genai
from PyPDF2 import PdfReader

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

SYLLABI_DIR = Path(__file__).parent / "syllabi"
GEMINI_MODEL = "gemini-3.6-flash"
FALL_TERM_YEAR = 2026

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

Example output:
[
  {{"Course": "CS 101", "Task": "Midterm Exam", "Due Date": "2026-03-15"}},
  {{"Course": "CS 101", "Task": "Final Project", "Due Date": "TBD"}}
]

Syllabus text:
{text}
"""


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract raw text from a PDF file."""
    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _normalize_pdf_text(text: str) -> str:
    """Collapse irregular PDF whitespace into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _course_from_filename(filename: str) -> str:
    """Infer course code like MATH 201 from MATH201_Syllabus.pdf."""
    stem = Path(filename).stem.upper()
    match = re.search(r"\b([A-Z]{2,5})(\d{3}[A-Z]?)\b", stem.replace("_", " "))
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return stem.replace("_", " ")


def _month_day_to_iso(month: str, day: str, year: int = FALL_TERM_YEAR) -> str:
    month_num = MONTH_MAP.get(month.lower()[:4].rstrip("t"), MONTH_MAP.get(month.lower()[:3]))
    if not month_num:
        raise ValueError(f"Unknown month: {month}")
    return f"{year}-{month_num:02d}-{int(day):02d}"


def _record(course: str, task: str, due_date: str) -> dict[str, Any]:
    return {
        "Source": "Syllabus",
        "Course": course,
        "Task": task,
        "Due Date": due_date,
    }


def parse_weekly_schedule_text(text: str, course: str) -> list[dict[str, Any]]:
    """Parse MATH-style tentative weekly schedule PDFs without an LLM."""
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
    """Extract exam entries from a standard syllabus PDF without an LLM."""
    normalized = _normalize_pdf_text(text)
    assignments: list[dict[str, Any]] = []

    if re.search(r"final exam", normalized, re.IGNORECASE):
        assignments.append(_record(course, "Final Exam", "TBD"))

    deferred = re.search(
        r"deferred final examination is scheduled as follows:\s*Date:\s*(\w+),?\s+(\d+)\s+(\w+),?\s+(\d{4})",
        normalized,
        re.IGNORECASE,
    )
    if deferred:
        _, day, month, year = deferred.groups()
        due = _month_day_to_iso(month, day, int(year))
        assignments.append(_record(course, "Deferred Final Exam", due))

    return assignments


def _is_weekly_schedule_pdf(pdf_path: Path) -> bool:
    name = pdf_path.name.lower()
    return "weekly" in name or "schedule" in name


def _is_syllabus_pdf(pdf_path: Path) -> bool:
    name = pdf_path.name.lower()
    return "syllabus" in name and "weekly" not in name and "schedule" not in name


def _parse_with_deterministic_parser(pdf_path: Path, text: str) -> list[dict[str, Any]] | None:
    """Try structured parsers before falling back to Gemini."""
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
    """Parse the LLM response into a list of assignment dicts."""
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


def _parse_single_pdf(
    pdf_path: Path,
    model: genai.GenerativeModel | None,
) -> list[dict[str, Any]]:
    """Extract and parse assignments from a single syllabus PDF."""
    logger.info("Parsing syllabus: %s", pdf_path.name)

    text = _extract_pdf_text(pdf_path)
    if not text:
        logger.warning("No text extracted from %s — skipping.", pdf_path.name)
        return []

    deterministic = _parse_with_deterministic_parser(pdf_path, text)
    if deterministic is not None:
        return deterministic

    if not model:
        logger.warning(
            "No Gemini model available and no deterministic parser matched %s.",
            pdf_path.name,
        )
        return []

    prompt = SYLLABUS_PROMPT.format(text=text[:30000])
    response = model.generate_content(prompt)
    raw = response.text if response.text else ""

    if not raw:
        logger.warning("Empty Gemini response for %s — skipping.", pdf_path.name)
        return []

    return _parse_gemini_response(raw)


def fetch_syllabus_assignments() -> list[dict[str, Any]]:
    """
    Iterate through PDFs in the syllabi/ folder and parse major assignments.

    Returns:
        List of dicts with keys: Source, Course, Task, Due Date.
        Individual PDF failures are logged and skipped.
    """
    SYLLABI_DIR.mkdir(exist_ok=True)
    pdf_files = sorted(SYLLABI_DIR.glob("*.pdf"))

    if not pdf_files:
        logger.info("No PDF files found in %s", SYLLABI_DIR)
        return []

    model: genai.GenerativeModel | None = None
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
    else:
        logger.warning("GEMINI_API_KEY not set — using deterministic parsers only.")

    all_assignments: list[dict[str, Any]] = []

    for pdf_path in pdf_files:
        try:
            assignments = _parse_single_pdf(pdf_path, model)
            all_assignments.extend(assignments)
            logger.info(
                "Parsed %d assignment(s) from %s.", len(assignments), pdf_path.name
            )
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON from Gemini for %s: %s", pdf_path.name, exc)
        except Exception as exc:
            logger.error("Failed to parse %s: %s", pdf_path.name, exc)

    logger.info("Total syllabus assignments parsed: %d", len(all_assignments))
    return all_assignments
