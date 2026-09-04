"""
Deterministic Task_ID generation and cross-source deduplication for Sylla Sync.

Unique keys:
  - Canvas:   canvas_{assignment_id}
  - Syllabus: syllabus_{clean(course)}_{clean(title)}_{due_date}

Cross-source rule: when Canvas and syllabus describe the same assignment
(course + fuzzy title, optionally same due date), keep Canvas and discard syllabus.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from config import BASE_DIR
from course_utils import normalize_course_code
from date_utils import normalize_calendar_date

logger = logging.getLogger(__name__)

STATE_PATH = BASE_DIR / ".syllasync_task_state.json"

NUMBERED_TASK_RE = re.compile(
    r"^(assignment|quiz|exam|lab|homework|hw|project|midterm|final)\s*#?\s*(\d+)\b",
    re.IGNORECASE,
)

TASK_ID_COL = "Task_ID"


@dataclass
class DedupStats:
    canvas_count: int = 0
    syllabus_count: int = 0
    discarded: int = 0
    kept: int = 0
    discarded_reasons: dict[str, int] = field(default_factory=dict)

    def bump(self, reason: str, n: int = 1) -> None:
        self.discarded += n
        self.discarded_reasons[reason] = self.discarded_reasons.get(reason, 0) + n


def clean_token(value: Any) -> str:
    """Normalize text for composite keys: lowercase, alnum only, underscores."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text or "unknown"


def normalize_title_text(title: Any) -> str:
    """Lowercase title with punctuation collapsed for fuzzy comparison."""
    text = str(title or "").strip().lower()
    text = re.sub(r"[^\w\s#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fuzzy_title_key(title: Any) -> str:
    """
    Collapse near-duplicate titles (Assignment #1: Long vs Assignment #1: Short).
    """
    title_norm = normalize_title_text(title)
    numbered = NUMBERED_TASK_RE.match(title_norm)
    if numbered:
        kind = numbered.group(1).lower()
        if kind == "hw":
            kind = "homework"
        return f"{kind}#{numbered.group(2)}"
    return title_norm


def fuzzy_match_key(course: Any, title: Any) -> str:
    """Stable course+title fingerprint used for cross-source and sheet matching."""
    course_raw = str(course or "").strip()
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


def ensure_task_id(record: dict[str, Any]) -> str:
    """Return existing Task_ID or build one from source fields."""
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


def load_task_state() -> dict[str, Any]:
    """Load persistent Task_ID map keyed by fuzzy_match_key."""
    if not STATE_PATH.exists():
        return {"by_match_key": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"by_match_key": {}}
        data.setdefault("by_match_key", {})
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read task state file: %s", exc)
        return {"by_match_key": {}}


def save_task_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write task state file: %s", exc)


def remember_task_ids(df: pd.DataFrame, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist Task_ID (+ manual fields) by fuzzy match key for future runs."""
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
        entry["task_id"] = task_id
        for col in ("Status", "Priority", "Estimated time dedicated to task"):
            val = str(row.get(col, "") or "").strip()
            if val:
                entry[col] = val
        by_key[key] = entry

    save_task_state(state)
    return state


def resolve_persisted_task_id(course: Any, title: Any, fallback: str, state: dict[str, Any]) -> str:
    """Prefer a previously saved Task_ID for this fuzzy assignment identity."""
    key = fuzzy_match_key(course, title)
    entry = state.get("by_match_key", {}).get(key) or {}
    saved = str(entry.get("task_id") or "").strip()
    return saved or fallback


def dedupe_cross_source(
    canvas_data: list[dict[str, Any]],
    syllabus_data: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], DedupStats]:
    """
    Attach Task_IDs and collapse Canvas∩Syllabus duplicates (Canvas wins).

    Returns normalized records with keys:
      Source, Course, Task, Due Date, Task_ID, (optional Canvas ID)
    """
    stats = DedupStats(canvas_count=len(canvas_data), syllabus_count=len(syllabus_data))
    state = load_task_state()
    kept: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    canvas_fuzzy_keys: set[str] = set()
    canvas_fuzzy_date_keys: set[str] = set()

    # --- Canvas first (authoritative) ---
    for item in canvas_data:
        course = item.get("Course", "")
        title = item.get("Task", "")
        due = item.get("Due Date", "")
        canvas_id = item.get("Canvas ID") or item.get("assignment_id")

        task_id = (
            canvas_task_id(canvas_id)
            if canvas_id not in (None, "")
            else syllabus_task_id(course, title, due)
        )
        task_id = resolve_persisted_task_id(course, title, task_id, state)

        fuzzy = fuzzy_match_key(course, title)
        due_iso = normalize_calendar_date(due)
        canvas_fuzzy_keys.add(fuzzy)
        if due_iso:
            canvas_fuzzy_date_keys.add(f"{fuzzy}||{due_iso}")

        if task_id in seen_task_ids:
            stats.bump("canvas_duplicate_task_id")
            continue

        seen_task_ids.add(task_id)
        kept.append(
            {
                "Source": "Canvas",
                "Course": course,
                "Task": title,
                "Due Date": due,
                "Canvas ID": canvas_id or "",
                TASK_ID_COL: task_id,
            }
        )

    # --- Syllabus: drop if overlaps Canvas ---
    for item in syllabus_data:
        course = item.get("Course", "")
        title = item.get("Task", "")
        due = item.get("Due Date", "")
        fuzzy = fuzzy_match_key(course, title)
        due_iso = normalize_calendar_date(due)

        if fuzzy in canvas_fuzzy_keys:
            stats.bump("syllabus_overlaps_canvas")
            continue
        if due_iso and f"{fuzzy}||{due_iso}" in canvas_fuzzy_date_keys:
            stats.bump("syllabus_overlaps_canvas_date")
            continue

        task_id = syllabus_task_id(course, title, due)
        task_id = resolve_persisted_task_id(course, title, task_id, state)

        if task_id in seen_task_ids:
            stats.bump("syllabus_duplicate_task_id")
            continue

        # Also collapse syllabus-only near-dupes by fuzzy key within this run
        if any(fuzzy_match_key(r["Course"], r["Task"]) == fuzzy for r in kept):
            stats.bump("syllabus_fuzzy_duplicate")
            continue

        seen_task_ids.add(task_id)
        kept.append(
            {
                "Source": "Syllabus",
                "Course": course,
                "Task": title,
                "Due Date": due,
                "Canvas ID": "",
                TASK_ID_COL: task_id,
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
    """Map deduped source records into tracker columns including Task_ID."""
    rows = [
        {
            "Course": r.get("Course", ""),
            "Assessment title": r.get("Task", ""),
            "Due Date": r.get("Due Date", ""),
            TASK_ID_COL: r.get(TASK_ID_COL, ""),
            "Source": r.get("Source", ""),
        }
        for r in records
    ]
    cols = ["Course", "Assessment title", "Due Date", TASK_ID_COL, "Source"]
    return pd.DataFrame(rows, columns=cols)


def print_dedup_stats(stats: DedupStats) -> None:
    print(
        f"[INFO] Retrieved: {stats.canvas_count} Canvas items, "
        f"{stats.syllabus_count} Syllabus items"
    )
    print(f"[INFO] Discarded: {stats.discarded} duplicate entries")
    if stats.discarded_reasons:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(stats.discarded_reasons.items()))
        print(f"[INFO] Discard detail: {detail}")
    print(f"[INFO] Kept after dedupe: {stats.kept} unique assignment(s)")
    logger.info(
        "Dedupe: canvas=%d syllabus=%d discarded=%d kept=%d (%s)",
        stats.canvas_count,
        stats.syllabus_count,
        stats.discarded,
        stats.kept,
        stats.discarded_reasons,
    )
