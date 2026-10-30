"""Parse syllabus PDFs using deterministic parsers and optional Gemini (google-genai)."""

from __future__ import annotations

import json
import logging
from safe_text import redact_text
import re
from pathlib import Path
from typing import Any

from PyPDF2 import PdfReader

from config import GEMINI_API_KEY, GEMINI_MODEL, TERM_YEAR
from course_utils import normalize_course_code
from date_utils import coerce_to_term_year, split_title_and_due
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
If the syllabus omits the year, this is the Fall {term_year} term: September-December dates are in {term_year} and January-April dates are in {next_year}. Never infer the year from weekday names.
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
    raw_stem = Path(filename).stem.upper()
    stem = raw_stem.replace("_", "")
    if stem.startswith("MATE") and len(stem) >= 7:
        return f"MAT E {stem[4:7]}"
    # Try with separators kept first: "ECE202_Syllabus" -> "ECE202 SYLLABUS"
    # so the catalog code ends at a word boundary. Collapsing the underscore
    # ("ECE202SYLLABUS") used to fall through to the fallback and yield
    # "ECE 202S" (the S of SYLLABUS glued onto the course number).
    spaced = re.sub(r"[_\-]+", " ", raw_stem)
    normalized = normalize_course_code(spaced) or normalize_course_code(stem)
    if normalized:
        return normalized
    # Optional section letter only when it is not the start of another word.
    match = re.match(r"([A-Z]+)(\d{3,4})([A-Z](?![A-Z]))?", stem)
    if match:
        return f"{match.group(1)} {match.group(2)}{match.group(3) or ''}"
    return Path(filename).stem.replace("_", " ")


def _month_day_to_iso(month: str, day: str, year: int | None = None) -> str:
    """Convert month/day to ISO; never emit 'None-MM-DD' when year is missing."""
    resolved_year = year if year is not None else TERM_YEAR
    month_num = MONTH_MAP.get(month.lower()[:4].rstrip("t"), MONTH_MAP.get(month.lower()[:3]))
    if not month_num:
        raise ValueError(f"Unknown month: {month}")
    return f"{resolved_year}-{month_num:02d}-{int(day):02d}"


_ORDINAL_EXAM = {
    "first": "1",
    "1st": "1",
    "second": "2",
    "2nd": "2",
    "third": "3",
    "3rd": "3",
    "fourth": "4",
    "4th": "4",
}

# Dated exam mention. The date group stops at the calendar day so a following
# clock ("5:30- 6:30pm") can be read separately and is not dropped.
_EXAM_MENTION_RE = re.compile(
    r"\b(?:(?P<ordinal>first|second|third|fourth|1st|2nd|3rd|4th)\s+)?"
    r"(?P<task>Midterm(?:\s*\d+(?![\d.])(?!\s*%))?|Final(?:\s*Exam)?)\b"
    # Grade weight ("Midterm 30 %", "Final 40%") is not part of the title.
    r"(?:\s*\d+(?:\.\d+)?\s*%)?"
    r"[^\n]{0,80}?"
    r"\b(?P<due>TBD|TBA|\d{4}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
    r"\s+\d{1,2}(?:,\s*\d{4})?)\b",
    re.IGNORECASE,
)

# "final exam period ... from December 9 to December 22" names a window, not
# the exam itself. Such mentions must not produce a dated exam row.
_EXAM_PERIOD_GAP_RE = re.compile(r"\bperiod\b", re.IGNORECASE)
_DATE_RANGE_AFTER_RE = re.compile(
    r"^\s*(?:to|until|through|thru|"
    r"[-\u2013\u2014]\s*(?=(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|\d{1,2}(?![\d:]|\s*[ap]\.?m)))\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?\d{1,2}\b",
    re.IGNORECASE,
)

# Clock immediately after the date. A range keeps the START. A meridian written
# only on the end ("5:30- 6:30pm") applies to that start; the end clock is not used.
_CLOCK_AFTER_DATE_RE = re.compile(
    r"^\s*[,.]?\s*(?:at\s+)?"
    r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<p1>[ap]\.?m\.?)?"
    r"(?:\s*[-–—]\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<p2>[ap]\.?m\.?)?)?",
    re.IGNORECASE,
)


def _minutes_24h(hour: int, minute: int, meridian: str) -> int:
    if meridian == "pm" and hour != 12:
        hour += 12
    elif meridian == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _hhmm_from_clock_match(match: re.Match[str]) -> str | None:
    """Return HH:MM for the start clock, or None when the suffix is not a time.

    When only the END of a range carries am/pm ("11-12:20 pm", "5:30- 6:30pm"),
    the start takes whichever meridiem puts it before the end with the
    shortest span: 11-12:20pm -> 11:00, 5:30-6:30pm -> 17:30.
    """
    if not any(match.group(name) for name in ("m1", "p1", "m2", "p2")):
        return None
    hour = int(match.group("h1"))
    minute = int(match.group("m1") or "0")
    if hour < 1 or hour > 12 or minute > 59:
        return None
    start_mer = (match.group("p1") or "").lower().replace(".", "")
    end_mer = (match.group("p2") or "").lower().replace(".", "")
    if start_mer in {"am", "pm"}:
        start = _minutes_24h(hour, minute, start_mer)
    elif end_mer in {"am", "pm"} and match.group("h2"):
        end_hour = int(match.group("h2"))
        end_minute = int(match.group("m2") or "0")
        if end_hour < 1 or end_hour > 12 or end_minute > 59:
            return None
        end = _minutes_24h(end_hour, end_minute, end_mer)
        candidates = [
            value
            for value in (
                _minutes_24h(hour, minute, "am"),
                _minutes_24h(hour, minute, "pm"),
            )
            if value < end
        ]
        if not candidates:
            return None
        start = max(candidates)
    else:
        return None
    return f"{start // 60:02d}:{start % 60:02d}"


def _end_hhmm_from_clock_match(match: re.Match[str]) -> str | None:
    """Return HH:MM for the END of a clock range, or None if no end clock."""
    if not match.group("h2"):
        return None
    end_hour = int(match.group("h2"))
    end_minute = int(match.group("m2") or "0")
    if end_hour < 1 or end_hour > 12 or end_minute > 59:
        return None
    end_mer = (match.group("p2") or match.group("p1") or "").lower().replace(".", "")
    if end_mer not in {"am", "pm"}:
        return None
    end = _minutes_24h(end_hour, end_minute, end_mer)
    return f"{end // 60:02d}:{end % 60:02d}"


def _due_and_end_with_following_clock(due: str, suffix: str) -> tuple[str, str]:
    """Return (due_with_start, end_date_or_empty) from a clock/range after the date."""
    due_out = _due_with_following_clock(due, suffix)
    if not due or due.upper() in {"TBD", "TBA"}:
        return due_out, ""
    clock = _CLOCK_AFTER_DATE_RE.match(suffix or "")
    if not clock:
        return due_out, ""
    end_hhmm = _end_hhmm_from_clock_match(clock)
    if not end_hhmm:
        return due_out, ""
    # Keep end on the same calendar day as the due date portion.
    day = due_out.split()[0] if due_out else due
    return due_out, f"{day} {end_hhmm}"


def _due_with_following_clock(due: str, suffix: str) -> str:
    """Append a clock that follows the date. Never invent a time that is not there."""
    if not due or due.upper() in {"TBD", "TBA"}:
        return due
    if re.search(r"\d{1,2}:\d{2}", due):
        return due
    clock = _CLOCK_AFTER_DATE_RE.match(suffix or "")
    if not clock:
        return due
    hhmm = _hhmm_from_clock_match(clock)
    if not hhmm:
        return due
    return f"{due} {hhmm}"


def _normalize_exam_task(raw_task: str, ordinal: str) -> str:
    task = raw_task.strip()
    if re.fullmatch(r"final(?:\s*exam)?", task, re.IGNORECASE):
        return "Final Exam"
    if re.match(r"midterm", task, re.IGNORECASE):
        numbered = re.search(r"(\d+)\s*$", task)
        if numbered:
            return f"Midterm {numbered.group(1)}"
        number = _ORDINAL_EXAM.get((ordinal or "").lower())
        if number:
            return f"Midterm {number}"
        return "Midterm"
    return task


def _record(
    course: str,
    task: str,
    due_date: str,
    end_date: str = "",
) -> dict[str, Any]:
    clean_task, clean_due = split_title_and_due(task, due_date)
    record: dict[str, Any] = {
        "Source": "Syllabus",
        "Course": course,
        "Task": clean_task,
        "Due Date": coerce_to_term_year(clean_due),
    }
    if end_date:
        _, clean_end = split_title_and_due(clean_task, end_date)
        if clean_end:
            record["End Date"] = coerce_to_term_year(clean_end)
    return record


def _loads_lenient(snippet: str) -> Any:
    """json.loads, then a second attempt with trailing commas removed."""
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", snippet)
        return json.loads(repaired)


def _salvage_objects(text: str) -> list[dict[str, Any]]:
    """Pull complete JSON objects out of a truncated model response."""
    objects: list[dict[str, Any]] = []
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def parse_model_payload(raw_text: str) -> list[dict[str, str]]:
    """
    Turn Gemini text into syllabus rows.

    Accepts fenced JSON, prose around an array, trailing commas, a single
    object, and truncated output that still contains complete objects.
    Items that are not objects are skipped.
    """
    if not str(raw_text or "").strip():
        raise ValueError("Gemini returned an empty response")

    cleaned = str(raw_text).strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    data: Any = None
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end > start:
        try:
            data = _loads_lenient(cleaned[start : end + 1])
        except json.JSONDecodeError:
            data = None
    if data is None and cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = _loads_lenient(cleaned)
        except json.JSONDecodeError:
            data = None
    if data is None:
        salvaged = _salvage_objects(cleaned)
        if not salvaged:
            raise ValueError(
                "Gemini response was not a JSON array of assignments. "
                "The raw response was not logged."
            )
        data = salvaged

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Gemini response is not a JSON array")

    results: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        task = item.get("Task") or item.get("task") or item.get("name")
        if not str(task or "").strip():
            continue
        course = item.get("Course") or item.get("course") or "Unknown"
        due = item.get("Due Date") or item.get("due_date") or item.get("due") or "TBD"
        clean_task, clean_due = split_title_and_due(task, due)
        if not str(clean_task or "").strip():
            continue
        results.append(
            {
                "Source": "Syllabus",
                "Course": str(course),
                "Task": clean_task,
                "Due Date": clean_due or "TBD",
            }
        )
    return results



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
        r"Midterm\s+(\d+)\s+Friday\s+(\w+)\s+(\d+)\s+at\s+"
        r"(5:30(?:\s*[-–—]\s*6:30)?\s*pm)",
        normalized,
        re.IGNORECASE,
    ):
        exam_num, month, day, time_frag = match.groups()
        due = _month_day_to_iso(month, day)
        clock = _CLOCK_AFTER_DATE_RE.match(time_frag.strip())
        start_hhmm = _hhmm_from_clock_match(clock) if clock else None
        end_hhmm = _end_hhmm_from_clock_match(clock) if clock else None
        start_hhmm = start_hhmm or "17:30"
        end_hhmm = end_hhmm or "18:30"
        assignments.append(
            _record(
                course,
                f"Midterm {exam_num}",
                f"{due} {start_hhmm}",
                end_date=f"{due} {end_hhmm}",
            )
        )

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

    def add(task: str, due: str, end: str = "") -> None:
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
        assignments.append(_record(course, task, due, end_date=end))

    for match in _EXAM_MENTION_RE.finditer(normalized):
        task = _normalize_exam_task(match.group("task") or "", match.group("ordinal") or "")
        due_text = (match.group("due") or "").strip()
        if due_text.upper() in {"TBD", "TBA"}:
            due = "TBD"
        elif re.match(r"\d{4}-\d{2}-\d{2}", due_text):
            due = due_text[:10]
        else:
            parts = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?", due_text)
            if parts:
                month, day, year = parts.groups()
                due = _month_day_to_iso(month, day, int(year) if year else None)
            else:
                due = "TBD"
        if due != "TBD":
            gap = normalized[match.end("task") : match.start("due")]
            following = normalized[match.end() :]
            if _EXAM_PERIOD_GAP_RE.search(gap) or _DATE_RANGE_AFTER_RE.match(following):
                # Exam period / date window, not the exam date. Leave the
                # exam undated (TBD draft is added below) instead of guessing.
                logger.info(
                    "Ignoring exam-period/date-range mention for %s %s: %r",
                    course,
                    task,
                    normalized[match.start() : match.end() + 30],
                )
                continue
            due, end = _due_and_end_with_following_clock(due, following)
            add(task, due, end)
        else:
            add(task, due)

    if re.search(r"final exam", normalized, re.IGNORECASE):
        add("Final Exam", "TBD")
    if re.search(r"\bmidterm\b", normalized, re.IGNORECASE):
        # A numbered midterm already covers this exam; do not also emit a bare draft.
        if not any("midterm" in key for key in seen_tasks):
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


# ---------------------------------------------------------------------------
# Canvas announcement text -> exam rows
# ---------------------------------------------------------------------------

_ANNOUNCE_EXAM_RE = re.compile(
    r"\b(?:(?P<ordinal>first|second|third|fourth|1st|2nd|3rd|4th)\s+)?"
    r"(?P<task>mid-?terms?(?:\s*(?:exam\s*)?#?\s*\d+)?"
    r"|final\s+exam(?:ination)?"
    r"|(?<=the\s)final\b(?!\s+(?:project|report|lab|grade|mark|assignment|presentation"
    r"|version|submission|answer|draft))"
    r"|exam\s*#?\s*\d+)",
    re.IGNORECASE,
)

_ANNOUNCE_DATE_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?!:)(?:,?\s*(?P<year>\d{4})\b)?",
    re.IGNORECASE,
)

# Sentences about something other than the sitting itself (review sessions,
# solutions, grades, deferred sittings, exam periods) are not exam dates.
_ANNOUNCE_SKIP_RE = re.compile(
    r"\b(?:review|practice|solutions?|grades?|graded|results?|returned|deferred"
    r"|period|office\s+hours?|regrade|re-grade|viewing|sample|past)\b",
    re.IGNORECASE,
)

_WEEKDAY_RE = re.compile(
    r"\b(Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)(?:day|nesday|sday|urday|rsday)?\b",
    re.IGNORECASE,
)
_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Split on sentence punctuation, but not on a month abbreviation ("Oct. 23").
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<!\bJan)(?<!\bFeb)(?<!\bMar)(?<!\bApr)(?<!\bJun)(?<!\bJul)(?<!\bAug)"
    r"(?<!\bSep)(?<!\bSept)(?<!\bOct)(?<!\bNov)(?<!\bDec)[.!?]+\s+|\n+",
    re.IGNORECASE,
)


def html_to_text(value: str) -> str:
    """Flatten Canvas HTML (announcement bodies) into plain text with line breaks."""
    import html as _html

    text = str(value or "")
    text = re.sub(r"(?i)<\s*br\s*/?>|</\s*(?:p|div|li|h[1-6]|tr)\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _announcement_task(raw_task: str, ordinal: str) -> str:
    task = re.sub(r"\s+", " ", raw_task.strip())
    if re.match(r"mid-?term", task, re.IGNORECASE):
        numbered = re.search(r"(\d+)\s*$", task)
        if numbered:
            return f"Midterm {numbered.group(1)}"
        number = _ORDINAL_EXAM.get((ordinal or "").lower())
        return f"Midterm {number}" if number else "Midterm"
    if re.match(r"final", task, re.IGNORECASE):
        return "Final Exam"
    numbered = re.search(r"(\d+)\s*$", task)
    return f"Exam {numbered.group(1)}" if numbered else "Exam"


def parse_announcement_exam_text(
    text: str,
    course: str,
    *,
    base_year: int | None = None,
    posted_month: int | None = None,
    source_ref: str = "",
) -> list[dict[str, Any]]:
    """
    Extract exam dates (midterm / final / exam N) from announcement text.

    Only sentences that name an exam AND an explicit month + day produce a row.
    A clock is attached only if one follows the date; otherwise the row stays
    date-only (all-day). Ambiguous sentences (several dates or several exams)
    are skipped rather than guessed.
    """
    course_label = normalize_course_code(course) or str(course or "").strip()
    year_default = base_year if base_year is not None else TERM_YEAR
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        exams = list(_ANNOUNCE_EXAM_RE.finditer(sentence))
        if not exams:
            continue
        dates = list(_ANNOUNCE_DATE_RE.finditer(sentence))
        if not dates:
            continue
        if _ANNOUNCE_SKIP_RE.search(sentence):
            logger.info("Announcement sentence skipped (not an exam sitting): %r", sentence)
            continue
        tasks = {_announcement_task(m.group("task"), m.group("ordinal") or "") for m in exams}
        resolved: list[tuple[str, re.Match[str]]] = []
        for date_match in dates:
            month_num = MONTH_MAP.get(date_match.group("month").lower()[:4].rstrip("t")) or MONTH_MAP.get(
                date_match.group("month").lower()[:3]
            )
            if not month_num:
                continue
            if date_match.group("year"):
                year = int(date_match.group("year"))
            else:
                year = year_default
                # Posted in the fall about a Jan-Apr date -> next calendar year.
                if posted_month and posted_month >= 9 and month_num <= 4:
                    year += 1
            resolved.append((_month_day_to_iso(date_match.group("month"), date_match.group("day"), year), date_match))
        distinct = {iso for iso, _ in resolved}
        if len(tasks) != 1 or len(distinct) != 1:
            logger.info(
                "Announcement sentence ambiguous (%d exam(s), %d date(s)) - skipped: %r",
                len(tasks),
                len(distinct),
                sentence,
            )
            continue
        task = next(iter(tasks))
        iso, date_match = resolved[0]
        weekday = _WEEKDAY_RE.search(sentence)
        if weekday:
            from datetime import date as _date

            stated = _WEEKDAY_INDEX.get(weekday.group(1).lower()[:3])
            actual = _date.fromisoformat(iso).weekday()
            if stated is not None and stated != actual:
                logger.warning(
                    "Announcement weekday mismatch for %s %s: %r -> %s is not a %s",
                    course_label,
                    task,
                    sentence,
                    iso,
                    weekday.group(0),
                )
        due, end = _due_and_end_with_following_clock(iso, sentence[date_match.end() :])
        key = (task.lower(), due)
        if key in seen:
            continue
        seen.add(key)
        row = {
            "Source": "Canvas Announcement",
            "Course": course_label,
            "Task": task,
            "Due Date": due,
            "Origin": source_ref,
            "Evidence": sentence,
        }
        if end:
            row["End Date"] = end
        rows.append(row)
    return rows


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


def _parse_gemini_response(
    raw_text: str, course_from_filename: str | None = None
) -> list[dict[str, str]]:
    """Parse Gemini JSON; prefer a catalog course code from the PDF filename."""
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError("Gemini response is not a JSON array")
    # Filename catalog code (e.g. MATH 209) beats Gemini-inferred titles
    # like "Multivariable Calculus" which do not normalize to a course code.
    catalog_from_file = normalize_course_code(course_from_filename or "")
    results: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        clean_task, clean_due = split_title_and_due(
            item.get("Task", "Unknown"),
            item.get("Due Date", "TBD"),
        )
        course = str(item.get("Course", "Unknown"))
        if catalog_from_file:
            course = catalog_from_file
        results.append(
            {
                "Source": "Syllabus",
                "Course": course,
                "Task": clean_task,
                "Due Date": coerce_to_term_year(clean_due) if clean_due else "TBD",
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

    prompt = SYLLABUS_PROMPT.format(
        text=text[:30000], term_year=TERM_YEAR, next_year=TERM_YEAR + 1
    )
    try:
        raw = _gemini_generate(client, prompt)
    except Exception as exc:  # noqa: BLE001
        logger.error("Gemini parse failed for %s: %s", pdf_path.name, exc)
        return []

    if not raw:
        logger.warning("Empty Gemini response for %s — skipping.", pdf_path.name)
        return []
    return _parse_gemini_response(
        raw, course_from_filename=_course_from_filename(pdf_path.name)
    )


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
