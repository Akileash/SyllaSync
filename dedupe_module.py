"""
Deterministic Task_ID generation, cross-source deduplication, and ID promotion.

Unique keys:
  - Canvas:   canvas_{assignment_id}
  - Syllabus: syllabus_{clean(course)}_{clean(title)}_{due_date}

Promotion rule: when a syllabus row fuzzy-matches a Canvas row, the canonical
Task_ID becomes canvas_{id} (and the old syllabus key is aliased in state).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from config import BASE_DIR
from date_utils import (
    apply_math209_online_monday_due,
    normalize_calendar_date,
    split_title_and_due,
)
from course_utils import courses_equivalent, normalize_course_code, prefer_course_label

logger = logging.getLogger(__name__)

STATE_PATH = BASE_DIR / ".syllasync_task_state.json"
TASK_ID_COL = "Task_ID"

NUMBERED_TASK_RE = re.compile(
    r"^(assignment|assign|assn|a|quiz|exam|lab|homework|hw|project|midterm|final|ps|problem\s*set)"
    r"\s*#?\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)

_WORD_NUMBERS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}

# Generic category placeholders — never real single due items (even with a date)
CATEGORY_PHANTOMS = {
    "assignments",
    "assignment",
    "quizzes",
    "quiz",
    "labs",
    "lab",
    "project",
    "projects",
    "homework",
    "homeworks",
    "term work",
    "online assignments",
}

# Exam labels that are only phantoms when undated / TBD
EXAM_PHANTOMS = {
    "midterm",
    "final",
    "final exam",
    "exams",
}

PHANTOM_TITLES = CATEGORY_PHANTOMS | EXAM_PHANTOMS


@dataclass
class DedupStats:
    canvas_count: int = 0
    syllabus_count: int = 0
    discarded: int = 0
    kept: int = 0
    promoted: int = 0
    discarded_reasons: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str, n: int = 1) -> None:
        self.discarded += n
        self.discarded_reasons[reason] = self.discarded_reasons.get(reason, 0) + n


def clean_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text or "unknown"


def normalize_title_text(title: Any) -> str:
    text = str(title or "").strip().lower()
    text = re.sub(r"[^\w\s#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fuzzy_title_key(title: Any) -> str:
    """Collapse near-duplicate titles into a stable fingerprint."""
    title_norm = normalize_title_text(title)
    # "Online Assignment 2 …" → same key as "Assignment 2"
    online = re.match(
        r"^online\s+(assignment|assign|quiz|lab|homework|hw|project)\s*#?\s*"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        title_norm,
        re.IGNORECASE,
    )
    if online:
        kind = {
            "hw": "homework",
            "assign": "assignment",
        }.get(online.group(1).lower(), online.group(1).lower())
        num = _WORD_NUMBERS.get(online.group(2).lower(), online.group(2).lower())
        return f"{kind}#{num}"

    numbered = NUMBERED_TASK_RE.match(title_norm)
    if numbered:
        kind = numbered.group(1).lower()
        kind = {
            "hw": "homework",
            "assn": "assignment",
            "assign": "assignment",
            "a": "assignment",
            "ps": "homework",
            "problem set": "homework",
        }.get(kind, kind)
        if kind == "final":
            kind = "exam"
        num = numbered.group(2).lower()
        num = _WORD_NUMBERS.get(num, num)
        return f"{kind}#{num}"

    if re.search(r"\bmidterms?\b", title_norm):
        num = re.search(r"\bmidterms?\s*#?\s*(\d+)\b", title_norm)
        return f"midterm#{num.group(1)}" if num else "midterm#1"

    if re.search(r"\bfinals?\b", title_norm) or re.search(r"\bfinal\s+exam\b", title_norm):
        return "exam#final"

    tokens = [
        t
        for t in title_norm.split()
        if t not in {"the", "a", "an", "and", "of", "to", "for"}
    ]
    return " ".join(tokens)


def fuzzy_match_key(course: Any, title: Any) -> str:
    course_raw = str(course or "").strip()
    # Always key on canonical code so MTH 201 / MATH 201W collapse together
    course_key = (normalize_course_code(course_raw) or course_raw).strip().lower()
    return f"{course_key}||{fuzzy_title_key(title)}"


def canvas_task_id(assignment_id: Any) -> str:
    return f"canvas_{assignment_id}"


def syllabus_task_id(course: Any, title: Any, due_date: Any) -> str:
    due = normalize_calendar_date(due_date) or clean_token(due_date)
    return (
        f"syllabus_{clean_token(normalize_course_code(str(course or '')) or course)}"
        f"_{clean_token(title)}_{due}"
    )


def is_phantom_task(title: Any, due_date: Any = None) -> bool:
    """
    True for bare heuristic placeholders (e.g. "Labs", "Assignments").

    Category buckets are always phantoms — a syllabus line like "Labs … Sep 8"
    is almost never a real assignment due that day (often a weight/% table hit).
    Bare Midterm/Final without a date are drafts; numbered items (Lab 3, Midterm 1)
    are kept.
    """
    title_norm = normalize_title_text(title)
    if not title_norm:
        return True
    # Numbered / specific tasks are real (Lab 3, Midterm 2, Assignment 1)
    if NUMBERED_TASK_RE.match(title_norm):
        return False
    if re.search(r"\bonline\s+assignments?\s*#?\s*\d+\b", title_norm):
        return False
    if re.search(r"\bmidterms?\s*#?\s*\d+\b", title_norm):
        return False
    if title_norm in CATEGORY_PHANTOMS:
        return True
    due_iso = normalize_calendar_date(due_date)
    if title_norm in EXAM_PHANTOMS and not due_iso:
        return True
    return False


def is_droppable_placeholder(title: Any, due_date: Any = "") -> bool:
    """
    Bare category headings that must never stay on Sheets/Calendar/Discord.

    Unlike undated Midterm/Final drafts, these are always removed — including
    stale rows already on Google Sheets from older syncs.
    """
    title_norm = normalize_title_text(title)
    if not title_norm:
        return True
    if NUMBERED_TASK_RE.match(title_norm):
        return False
    if re.search(r"\bonline\s+assignments?\s*#?\s*\d+\b", title_norm):
        return False
    return title_norm in CATEGORY_PHANTOMS


def dates_within(a: Any, b: Any, hours: int = 48) -> bool:
    """True if both parse and are within ±hours (default 48h)."""
    da = pd.to_datetime(a, errors="coerce")
    db = pd.to_datetime(b, errors="coerce")
    if pd.isna(da) or pd.isna(db):
        return False
    return abs((da.to_pydatetime() - db.to_pydatetime()).total_seconds()) <= hours * 3600


def load_task_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"by_match_key": {}, "aliases": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"by_match_key": {}, "aliases": {}}
        data.setdefault("by_match_key", {})
        data.setdefault("aliases", {})
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read task state file: %s", exc)
        return {"by_match_key": {}, "aliases": {}}


def save_task_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write task state file: %s", exc)


def hydrate_state_from_rows(rows: list[dict[str, Any]], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Rebuild local Task_ID map from Google Sheet / tracker rows.

    Used when .syllasync_task_state.json is missing so identity survives
    machine switches.
    """
    state = state or load_task_state()
    by_key = state.setdefault("by_match_key", {})
    for row in rows:
        course = row.get("Course", "")
        title = row.get("Assessment title") or row.get("Task", "")
        task_id = str(row.get(TASK_ID_COL) or "").strip()
        if not task_id:
            continue
        key = fuzzy_match_key(course, title)
        entry = by_key.get(key, {})
        # Prefer canvas_ IDs when hydrating
        existing = str(entry.get("task_id") or "")
        if existing.startswith("canvas_") and not task_id.startswith("canvas_"):
            continue
        entry["task_id"] = task_id
        for col in ("Status", "Priority", "Estimated time dedicated to task"):
            val = str(row.get(col, "") or "").strip()
            if val:
                entry[col] = val
        by_key[key] = entry
    save_task_state(state)
    return state


def promote_task_id(old_id: str, new_id: str, state: dict[str, Any]) -> None:
    """Record syllabus→canvas promotion alias and rewrite match-key entries."""
    if not old_id or not new_id or old_id == new_id:
        return
    aliases = state.setdefault("aliases", {})
    aliases[old_id] = new_id
    by_key = state.setdefault("by_match_key", {})
    for entry in by_key.values():
        if str(entry.get("task_id") or "") == old_id:
            entry["task_id"] = new_id


def resolve_persisted_task_id(
    course: Any,
    title: Any,
    preferred: str,
    state: dict[str, Any],
) -> str:
    """
    Resolve Task_ID with promotion awareness.

    Prefer canvas_* preferred IDs over stored syllabus_* keys (promotion).
    """
    aliases = state.get("aliases") or {}
    preferred = aliases.get(preferred, preferred)

    key = fuzzy_match_key(course, title)
    entry = (state.get("by_match_key") or {}).get(key) or {}
    saved = str(entry.get("task_id") or "").strip()
    saved = aliases.get(saved, saved)

    if preferred.startswith("canvas_"):
        if saved and saved != preferred and saved.startswith("syllabus_"):
            promote_task_id(saved, preferred, state)
        return preferred
    return saved or preferred


def remember_task_ids(df: pd.DataFrame, state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state or load_task_state()
    by_key: dict[str, Any] = state.setdefault("by_match_key", {})

    for _, row in df.iterrows():
        course = row.get("Course", "")
        title = row.get("Assessment title") or row.get("Task", "")
        key = fuzzy_match_key(course, title)
        if key == "||":
            continue
        entry = by_key.get(key, {})
        task_id = str(row.get(TASK_ID_COL) or entry.get("task_id") or "").strip()
        if not task_id:
            task_id = syllabus_task_id(course, title, row.get("Due Date", ""))
        # Don't downgrade canvas → syllabus
        existing = str(entry.get("task_id") or "")
        if existing.startswith("canvas_") and task_id.startswith("syllabus_"):
            task_id = existing
        entry["task_id"] = task_id
        for col in ("Status", "Priority", "Estimated time dedicated to task"):
            val = str(row.get(col, "") or "").strip()
            if val:
                entry[col] = val
        by_key[key] = entry

    save_task_state(state)
    return state


def ensure_task_id(record: dict[str, Any]) -> str:
    existing = str(record.get(TASK_ID_COL) or record.get("Unique_Key") or "").strip()
    if existing:
        return existing
    source = str(record.get("Source", "")).strip().lower()
    canvas_id = record.get("Canvas ID") or record.get("assignment_id")
    course = record.get("Course", "")
    title = record.get("Task") or record.get("Assessment title", "")
    due = record.get("Due Date", "")
    if source == "canvas" and canvas_id not in (None, ""):
        return canvas_task_id(canvas_id)
    return syllabus_task_id(course, title, due)


def _titles_fuzzy_equal(a: str, b: str) -> bool:
    return fuzzy_title_key(a) == fuzzy_title_key(b)


def dedupe_cross_source(
    canvas_data: list[dict[str, Any]],
    syllabus_data: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], DedupStats]:
    """Attach Task_IDs, promote syllabus→canvas, drop overlaps (Canvas wins)."""
    stats = DedupStats(canvas_count=len(canvas_data), syllabus_count=len(syllabus_data))
    state = load_task_state()
    kept: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    canvas_index: list[dict[str, Any]] = []

    for item in canvas_data:
        course = item.get("Course", "")
        title = item.get("Task", "")
        due = item.get("Due Date", "")
        title, due = split_title_and_due(title, due)
        due = apply_math209_online_monday_due(course, title, due)
        canvas_id = item.get("Canvas ID") or item.get("assignment_id")
        preferred = (
            canvas_task_id(canvas_id)
            if canvas_id not in (None, "")
            else syllabus_task_id(course, title, due)
        )
        before = ((state.get("by_match_key") or {}).get(fuzzy_match_key(course, title)) or {}).get(
            "task_id"
        )
        task_id = resolve_persisted_task_id(course, title, preferred, state)
        if (
            before
            and str(before).startswith("syllabus_")
            and task_id.startswith("canvas_")
        ):
            stats.promoted += 1

        if task_id in seen_task_ids:
            stats.bump("canvas_duplicate_task_id")
            continue
        seen_task_ids.add(task_id)
        canon_course = normalize_course_code(course) or course
        row = {
            "Source": "Canvas",
            "Course": canon_course,
            "Task": title,
            "Due Date": due,
            "Canvas ID": canvas_id or "",
            TASK_ID_COL: task_id,
            "Is Draft": False,
        }
        kept.append(row)
        canvas_index.append(row)

    for item in syllabus_data:
        course = item.get("Course", "")
        title = item.get("Task", "")
        due = item.get("Due Date", "")
        title, due = split_title_and_due(title, due)
        due = apply_math209_online_monday_due(course, title, due)
        phantom = is_phantom_task(title, due)
        # Category phantoms (bare "Labs", "Assignments") are never kept —
        # they pollute Discord/Calendar even when a nearby PDF date was attached.
        if phantom and normalize_title_text(title) in CATEGORY_PHANTOMS:
            stats.bump("syllabus_category_phantom")
            continue

        # Cross-source overlap: same fuzzy title, or same fuzzy + close dates
        overlap = False
        for c_row in canvas_index:
            if not courses_equivalent(str(c_row["Course"]), str(course)):
                continue
            if _titles_fuzzy_equal(c_row["Task"], title):
                overlap = True
                break
            # Close-date proximity with shared token
            if dates_within(c_row["Due Date"], due, hours=48):
                c_tokens = set(normalize_title_text(c_row["Task"]).split())
                s_tokens = set(normalize_title_text(title).split())
                if c_tokens & s_tokens:
                    overlap = True
                    break
        if overlap:
            stats.bump("syllabus_overlaps_canvas")
            continue

        task_id = syllabus_task_id(course, title, due)
        task_id = resolve_persisted_task_id(course, title, task_id, state)
        if task_id in seen_task_ids:
            stats.bump("syllabus_duplicate_task_id")
            continue
        if any(fuzzy_match_key(r["Course"], r["Task"]) == fuzzy_match_key(course, title) for r in kept):
            stats.bump("syllabus_fuzzy_duplicate")
            continue

        seen_task_ids.add(task_id)
        kept.append(
            {
                "Source": "Syllabus",
                "Course": normalize_course_code(course) or course,
                "Task": title,
                "Due Date": due,
                "Canvas ID": "",
                TASK_ID_COL: task_id,
                "Is Draft": phantom,
            }
        )

    stats.kept = len(kept)
    remember_task_ids(
        pd.DataFrame(
            [
                {
                    "Course": r["Course"],
                    "Assessment title": r["Task"],
                    "Due Date": r["Due Date"],
                    TASK_ID_COL: r[TASK_ID_COL],
                }
                for r in kept
            ]
        ),
        state,
    )
    return kept, stats


def records_to_tracker_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [
        {
            "Course": r.get("Course", ""),
            "Assessment title": r.get("Task", ""),
            "Due Date": r.get("Due Date", ""),
            TASK_ID_COL: r.get(TASK_ID_COL, ""),
            "Source": r.get("Source", ""),
            "Is Draft": bool(r.get("Is Draft", False)),
        }
        for r in records
    ]
    cols = ["Course", "Assessment title", "Due Date", TASK_ID_COL, "Source", "Is Draft"]
    return pd.DataFrame(rows, columns=cols)


def print_dedup_stats(stats: DedupStats) -> None:
    print(
        f"[INFO] Retrieved: {stats.canvas_count} Canvas items, "
        f"{stats.syllabus_count} Syllabus items"
    )
    print(f"[INFO] Discarded: {stats.discarded} duplicate entries")
    if stats.promoted:
        print(f"[INFO] Promoted: {stats.promoted} syllabus Task_ID(s) -> canvas_*")
    if stats.discarded_reasons:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(stats.discarded_reasons.items()))
        print(f"[INFO] Discard detail: {detail}")
    print(f"[INFO] Kept after dedupe: {stats.kept} unique assignment(s)")
    logger.info(
        "Dedupe: canvas=%d syllabus=%d discarded=%d promoted=%d kept=%d (%s)",
        stats.canvas_count,
        stats.syllabus_count,
        stats.discarded,
        stats.promoted,
        stats.kept,
        stats.discarded_reasons,
    )
