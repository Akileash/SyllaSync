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


# Incoming free-text → canonical Status
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
    "graded": Status.GRADED,
    "cancelled": Status.CANCELLED,
    "canceled": Status.CANCELLED,
    "dropped": Status.CANCELLED,
    "complete": Status.COMPLETE,
    "completed": Status.COMPLETE,
    "done": Status.COMPLETE,
}

# Canonical → Google Sheets / HHS template dropdown
STATUS_TO_SHEET: dict[Status, str] = {
    Status.TODO: "Not Started",
    Status.IN_PROGRESS: "In Progress",
    Status.SUBMITTED: "Submitted",
    Status.GRADED: "Complete",
    Status.CANCELLED: "N/A",
    Status.COMPLETE: "Complete",
}

# Canonical → local Excel dropdown
STATUS_TO_EXCEL: dict[Status, str] = {
    Status.TODO: "Not started",
    Status.IN_PROGRESS: "In Progress",
    Status.SUBMITTED: "Done",
    Status.GRADED: "Done",
    Status.CANCELLED: "Done",
    Status.COMPLETE: "Done",
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


def sheet_status(value: object) -> str:
    return STATUS_TO_SHEET[normalize_status(value)]


def excel_status(value: object) -> str:
    return STATUS_TO_EXCEL[normalize_status(value)]


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
    if st in {Status.COMPLETE, Status.GRADED}:
        return "[DONE] "
    if st == Status.SUBMITTED:
        return "[SUBMITTED] "
    if st == Status.CANCELLED:
        return "[CANCELLED] "
    if st == Status.IN_PROGRESS:
        return "[WIP] "
    return ""
