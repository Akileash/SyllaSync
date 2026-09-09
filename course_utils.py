"""Course code matching and normalization for Sylla Sync."""

from __future__ import annotations

import re
from collections.abc import Callable

# Subject abbreviations that should collapse to a canonical department code.
# Edge case: Canvas/syllabus may say "MTH 201" while the catalog is "MATH 201".
SUBJECT_ALIASES: dict[str, str] = {
    "MTH": "MATH",
    "MATHS": "MATH",
    "MATHEMATICS": "MATH",
    "MATHE": "MAT E",
    "MATE": "MAT E",
    "ENG": "ENGG",
    "ENGR": "ENGG",
    "ENGINEERING": "ENGG",
}

# Prefer specific faculty patterns, then a generic alphanumeric course code.
# Number group allows an optional trailing letter (201W) which we strip later.
COURSE_CODE_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (
        re.compile(r"\bMAT\s*E\s*(\d{3,4})[A-Z]?\b", re.IGNORECASE),
        lambda m: f"MAT E {m.group(1)}",
    ),
    (
        re.compile(r"\b(ENGG|ENG|ENGR)\s*(\d{3,4})[A-Z]?\b", re.IGNORECASE),
        lambda m: f"ENGG {m.group(2)}",
    ),
    (
        re.compile(r"\b(ECE)\s*(\d{3,4})[A-Z]?\b", re.IGNORECASE),
        lambda m: f"ECE {m.group(2)}",
    ),
    (
        re.compile(r"\b(MATH|MTH|MATHS)\s*(\d{3,4})[A-Z]?\b", re.IGNORECASE),
        lambda m: f"MATH {m.group(2)}",
    ),
    # Generic: CS 101, CHEM 105, STAT 151, ENG 101A, etc.
    (
        re.compile(r"\b([A-Z]{2,5})\s*(\d{3,4})[A-Z]?\b", re.IGNORECASE),
        lambda m: _format_generic(m.group(1), m.group(2)),
    ),
]


def _format_generic(subject: str, number: str) -> str:
    subj = SUBJECT_ALIASES.get(subject.upper(), subject.upper())
    return f"{subj} {number}"


def parse_allowed_courses(value: str | None) -> list[str]:
    """Parse a comma-separated ALLOWED_COURSES env value."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_course_code(course_name: str) -> str | None:
    """
    Extract a canonical course code like 'MATH 201' from a Canvas/syllabus title.

    Collapses common variants:
      - MTH 201 / MATH 201W / Math 201 → MATH 201
      - MAT E 201 / MATE201 → MAT E 201
    """
    if not course_name or not str(course_name).strip():
        return None

    text = re.sub(r"\s+", " ", str(course_name).strip())
    for pattern, formatter in COURSE_CODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return formatter(match).upper()
    return None


def courses_equivalent(a: str | None, b: str | None) -> bool:
    """True when two course labels refer to the same catalog course."""
    na = normalize_course_code(a or "")
    nb = normalize_course_code(b or "")
    if na and nb:
        return na == nb
    return str(a or "").strip().upper() == str(b or "").strip().upper()


def prefer_course_label(current: str, candidate: str) -> str:
    """
    Prefer the canonical catalog-style label (MATH 201 over MTH 201 / MATH 201W).
    """
    cur = str(current or "").strip()
    cand = str(candidate or "").strip()
    canon_cur = normalize_course_code(cur)
    canon_cand = normalize_course_code(cand)

    if canon_cand and (not canon_cur or canon_cand == canon_cur):
        # Prefer exact canonical spelling when either side is messy
        if cand.upper() == canon_cand:
            return canon_cand
        if cur.upper() == canon_cur:
            return canon_cur or cur
        return canon_cand or cand or cur
    if canon_cur:
        return canon_cur if cur.upper() != canon_cur else cur
    return cur or cand


def course_matches_allowed(course_name: str, allowed_courses: list[str]) -> bool:
    """Return True if the Canvas course matches any allowed tracker class."""
    if not allowed_courses:
        return True

    normalized = normalize_course_code(course_name)
    allowed_normalized = {
        normalize_course_code(c) or c.upper() for c in allowed_courses
    }

    if normalized and normalized in allowed_normalized:
        return True

    name_upper = course_name.upper()
    for allowed in allowed_courses:
        if courses_equivalent(course_name, allowed):
            return True
        if allowed.upper() in name_upper:
            return True
    return False


def compact_course_code(course_name: str) -> str:
    """Return a filename-safe compact code like MATH201 or MATE201."""
    normalized = normalize_course_code(course_name)
    if not normalized:
        slug = re.sub(r"[^\w]+", "_", course_name).strip("_")
        return slug[:40] or "course"
    return normalized.replace(" ", "")
