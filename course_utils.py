"""Course code matching and normalization for Sylla Sync."""

from __future__ import annotations

import re

# Maps Canvas titles to canonical tracker class names.
COURSE_CODE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bMAT\s*E\s*(\d{3})\b", re.IGNORECASE), "MAT E {0}"),
    (re.compile(r"\b(ENGG)\s*(\d{3})\b", re.IGNORECASE), "{0} {1}"),
    (re.compile(r"\b(ECE)\s*(\d{3})\b", re.IGNORECASE), "{0} {1}"),
    (re.compile(r"\b(MATH)\s*(\d{3})\b", re.IGNORECASE), "{0} {1}"),
]


def parse_allowed_courses(value: str | None) -> list[str]:
    """Parse a comma-separated ALLOWED_COURSES env value."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_course_code(course_name: str) -> str | None:
    """Extract a canonical course code like 'ECE 210' from a Canvas title."""
    text = re.sub(r"\s+", " ", course_name.strip())
    for pattern, template in COURSE_CODE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if "{0}" in template and "{1}" not in template:
            return template.format(match.group(1)).upper().replace("MAT E", "MAT E")
        return template.format(match.group(1), match.group(2)).upper()
    return None


def course_matches_allowed(course_name: str, allowed_courses: list[str]) -> bool:
    """Return True if the Canvas course matches any allowed tracker class."""
    if not allowed_courses:
        return True

    normalized = normalize_course_code(course_name)
    allowed_normalized = {normalize_course_code(c) or c.upper() for c in allowed_courses}

    if normalized and normalized in allowed_normalized:
        return True

    name_upper = course_name.upper()
    for allowed in allowed_courses:
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
