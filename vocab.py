"""
Canonical Status / Priority vocabulary for Sylla Sync consumers.

Maps freely between Excel, Google Sheets template labels, Discord, and Calendar.
"""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    GRADED = "GRADED"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Incoming free-text -> canonical Status
_STATUS_ALIASES: dict[str, Status] = {
    "": Status.TODO,
    "todo": Status.TODO,
    "not started": Status.TODO,
    "not_started": Status.TODO,
    "n/a": Status.TODO,
    "in progress": Status.IN_PROGRESS,
    "in_progress": Status.IN_PROGRESS,
    "started": Status.IN_PROGRESS,
    "submitted": Status.SUBMITTED,
    "turned in": Status.SUBMITTED,
    # Graded stays its own enum so typed "Graded" can be preserved on the Sheet;
    # pipeline sheet_status still emits Submitted for Graded.
    "graded": Status.GRADED,
    "cancelled": Status.CANCELLED,
    "canceled": Status.CANCELLED,
    "dropped": Status.CANCELLED,
    "complete": Status.SUBMITTED,
    "completed": Status.SUBMITTED,
    # Done means finished work -> Submitted on the Sheet (via sheet_status).
    "done": Status.SUBMITTED,
}

# Canonical -> Google Sheets / HHS template dropdown
STATUS_TO_SHEET: dict[Status, str] = {
    Status.TODO: "Not Started",
    Status.IN_PROGRESS: "In Progress",
    Status.SUBMITTED: "Submitted",
    Status.GRADED: "Submitted",  # standing rule: finished items are "Submitted"
    Status.CANCELLED: "N/A",
    Status.COMPLETE: "Submitted",
}

# Canonical -> local Excel dropdown
STATUS_TO_EXCEL: dict[Status, str] = {
    Status.TODO: "Not started",
    Status.IN_PROGRESS: "In Progress",
    Status.SUBMITTED: "Done",
    Status.GRADED: "Done",
    Status.CANCELLED: "Done",
    Status.COMPLETE: "Done",
}

# Labels the Sheet dropdown accepts. Graded stays so existing validation
# does not flag cells the user already typed. Complete is accepted for
# read-back only (pipeline never writes it).
SHEET_STATUS_LABELS = (
    "Not Started",
    "In Progress",
    "Submitted",
    "Cancelled",
    "N/A",
)

_ACCEPTED_SHEET_LABELS: dict[str, str] = {
    label.lower(): label for label in SHEET_STATUS_LABELS
}

_PRIORITY_ALIASES: dict[str, Priority] = {
    "": Priority.MEDIUM,
    "low": Priority.LOW,
    "p 3": Priority.LOW,
    "p3": Priority.LOW,
    "3": Priority.LOW,
    "medium": Priority.MEDIUM,
    "med": Priority.MEDIUM,
    "p 2": Priority.MEDIUM,
    "p2": Priority.MEDIUM,
    "2": Priority.MEDIUM,
    "high": Priority.HIGH,
    "p 1": Priority.HIGH,
    "p1": Priority.HIGH,
    "1": Priority.HIGH,
    "critical": Priority.CRITICAL,
    "urgent": Priority.CRITICAL,
}

PRIORITY_TO_EXCEL: dict[Priority, str] = {
    Priority.LOW: "P 3",
    Priority.MEDIUM: "P 2",
    Priority.HIGH: "P 1",
    Priority.CRITICAL: "P 1",
}

PRIORITY_TO_DASHBOARD: dict[Priority, str] = {
    Priority.LOW: "Low",
    Priority.MEDIUM: "Medium",
    Priority.HIGH: "High",
    Priority.CRITICAL: "High",
}

# Google Calendar colorId
PRIORITY_TO_COLOR: dict[Priority, str] = {
    Priority.LOW: "9",       # Blue
    Priority.MEDIUM: "5",    # Yellow
    Priority.HIGH: "11",     # Red
    Priority.CRITICAL: "11",
}

STATUS_TO_COLOR: dict[Status, str] = {
    Status.TODO: "9",
    Status.IN_PROGRESS: "5",
    Status.SUBMITTED: "2",   # Green
    Status.GRADED: "2",
    Status.COMPLETE: "2",
    Status.CANCELLED: "8",   # Grey
}


def normalize_status(value: object) -> Status:
    key = str(value or "").strip().lower()
    return _STATUS_ALIASES.get(key, Status.TODO)


def normalize_priority(value: object) -> Priority:
    key = str(value or "").strip().lower()
    return _PRIORITY_ALIASES.get(key, Priority.MEDIUM)


def _forbid_complete(label: str) -> str:
    """Last-line guard so a pipeline status write can never be the word Complete."""
    if str(label).strip().lower() in {"complete", "completed"}:
        return "Submitted"
    return label


def sheet_status(value: object) -> str:
    """
    Sheet status text. Standing rule: the pipeline never marks anything
    Complete; finished items are "Submitted". A value typed as "Complete" on
    the Sheet is kept as typed via preserve_sheet_label, but aliases (Done,
    Graded) map to Submitted when the pipeline writes.
    """
    raw = str(value or "").strip()
    st = normalize_status(value)
    if st == Status.COMPLETE and raw.lower() in ("complete", "completed"):
        # Literal Complete on the Sheet: keep for read paths; writers use preserve/forbid.
        return STATUS_TO_SHEET[Status.COMPLETE]
    if st == Status.COMPLETE:
        return STATUS_TO_SHEET[Status.SUBMITTED]
    return STATUS_TO_SHEET[st]


def excel_status(value: object) -> str:
    return _forbid_complete(STATUS_TO_EXCEL[normalize_status(value)])


# How "finished" a status is; a merge never moves an item down this ladder.
# Submitted / Graded / Complete share one rank, so ties keep the current
# (Sheet) value: a stale or duplicate "Complete" can never override a
# "Submitted" on the Sheet, and a typed "Complete" is not rewritten either.
_STATUS_RANK: dict[Status, int] = {
    Status.TODO: 0,
    Status.IN_PROGRESS: 1,
    Status.SUBMITTED: 2,
    Status.GRADED: 2,
    Status.COMPLETE: 2,
    Status.CANCELLED: 2,
}


def status_rank(value: object) -> int:
    """Not Started < In Progress < finished statuses (those tie) < Cancelled."""
    return _STATUS_RANK[normalize_status(value)]


def preserve_sheet_label(value: object) -> str:
    """
    Keep a label the user typed when the dropdown accepts it.

    Graded, Complete, and Done are mapped to Submitted (standing rule).
    """
    key = str(value or "").strip().lower()
    if not key:
        return "Not Started"
    if key in _ACCEPTED_SHEET_LABELS:
        return _ACCEPTED_SHEET_LABELS[key]
    return sheet_status(value)


def choose_status(sheet_value: object, synced_value: object) -> str:
    """
    Pick the status to keep.

    Equal ranks keep the Sheet label. A higher rank may replace it.
    Pipeline writes never emit Complete; typed Complete on the Sheet is kept
    when ranks tie.
    """
    sheet = str(sheet_value or "").strip()
    synced = str(synced_value or "").strip()
    if not sheet:
        return _forbid_complete(sheet_status(synced) if synced else "Not Started")
    if not synced or status_rank(sheet) >= status_rank(synced):
        return preserve_sheet_label(sheet)
    return _forbid_complete(sheet_status(synced))


def excel_priority(value: object) -> str:
    return PRIORITY_TO_EXCEL[normalize_priority(value)]


def calendar_color(status: object, priority: object) -> str:
    """Prefer status color when complete/cancelled; else priority color."""
    st = normalize_status(status)
    if st in {Status.COMPLETE, Status.GRADED, Status.SUBMITTED, Status.CANCELLED}:
        return STATUS_TO_COLOR[st]
    return PRIORITY_TO_COLOR[normalize_priority(priority)]


def calendar_summary_prefix(status: object) -> str:
    st = normalize_status(status)
    if st == Status.SUBMITTED:
        return "[SUBMITTED] "
    if st in {Status.COMPLETE, Status.GRADED}:
        # Finished aliases share the Submitted calendar prefix.
        return "[SUBMITTED] "
    if st == Status.CANCELLED:
        return "[CANCELLED] "
    if st == Status.IN_PROGRESS:
        return "[WIP] "
    return ""


def prefer_status(current: object, candidate: object) -> str:
    """Return whichever raw status value is further along (ties keep current)."""
    cur = str(current or "").strip()
    cand = str(candidate or "").strip()
    if not cur:
        return cand
    if not cand:
        return cur
    if _STATUS_RANK[normalize_status(cand)] > _STATUS_RANK[normalize_status(cur)]:
        return cand
    return cur
