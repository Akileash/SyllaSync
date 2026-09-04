"""
Push assignment data to the HHS Assignment Tracker Masterlist in Google Sheets.

Requires:
  - GOOGLE_SHEET_ID in .env / local.env
  - credentials.json service-account key in the project root
  - Sheet shared with the service account email (Editor access)

Integration targets the **Masterlist** tab only. Columns I onward (formulas,
charts, calendars) are never modified.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import gspread
import pandas as pd
from gspread.exceptions import APIError, SpreadsheetNotFound

from config import BASE_DIR, GOOGLE_SHEET_ID
from date_utils import format_internal_datetime, format_sheet_date, format_sheet_time
from dedupe_module import (
    TASK_ID_COL,
    ensure_task_id,
    load_task_state,
    print_dedup_stats,
    remember_task_ids,
    resolve_persisted_task_id,
    syllabus_task_id,
)
from sheets_module import _normalize_key

logger = logging.getLogger(__name__)

# Sylla Sync internal schema (used for merge + Discord digest)
DISPLAY_COLUMNS = [
    "Course",
    "Assessment title",
    "Due Date",
    "Estimated time dedicated to task",
    "Status",
    "Priority",
]
COLUMNS = DISPLAY_COLUMNS + [TASK_ID_COL]

# HHS Assignment Tracker — Masterlist layout
MASTERLIST_SHEET = "Masterlist"
DATA_START_ROW = 11

# 1-based column indices on Masterlist (A=1)
COL_STATUS = 2       # B
COL_DUE_DATE = 3     # C  (visible "DUE DATE" header)
COL_DUE_DATE_CALC = 4  # D  (date value used by =DAYS(D{n}, ...) formulas)
COL_DUE_TIME = 5     # E
COL_CLASS = 6        # F
COL_TYPE = 7         # G
COL_ASSIGNMENT = 8   # H

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Template status values (from Programming tab defaults)
TEMPLATE_STATUS_OPTIONS = {
    "not started": "Not Started",
    "in progress": "In Progress",
    "completed": "Complete",
    "complete": "Complete",
    "done": "Complete",
    "submitted": "Submitted",
    "n/a": "N/A",
}

# Infer assignment TYPE (column G) from the title
TYPE_KEYWORDS: list[tuple[str, str]] = [
    (r"\b(midterm|final\s*exam|finals?)\b", "Exam"),
    (r"\bexam\b", "Exam"),
    (r"\bquiz\b", "Quiz"),
    (r"\b(project)\b", "Project"),
    (r"\b(paper|essay)\b", "Paper"),
    (r"\b(lab\s*report|prelab|lab)\b", "Lab Report"),
    (r"\b(presentation)\b", "Presentation"),
    (r"\b(reading)\b", "Reading"),
    (r"\b(homework|hw)\b", "Homework"),
]


class GoogleSheetPermissionError(PermissionError):
    """Raised when the service account cannot access the target spreadsheet."""


def _authenticate() -> gspread.Client:
    """Authenticate with the service-account credentials file."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Download a service-account key from Google Cloud Console."
        )

    if not GOOGLE_SHEET_ID:
        raise ValueError("GOOGLE_SHEET_ID must be set in .env or local.env")

    return gspread.service_account(filename=str(CREDENTIALS_PATH), scopes=SCOPES)


def _open_masterlist(client: gspread.Client) -> gspread.Worksheet:
    """Open the Masterlist worksheet."""
    try:
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        return spreadsheet.worksheet(MASTERLIST_SHEET)
    except SpreadsheetNotFound as exc:
        raise GoogleSheetPermissionError(
            "Spreadsheet not found. Verify GOOGLE_SHEET_ID and ensure the sheet "
            "is shared with your service account email as Editor."
        ) from exc
    except gspread.WorksheetNotFound as exc:
        raise GoogleSheetPermissionError(
            f"Worksheet '{MASTERLIST_SHEET}' not found. "
            "Ensure you are using the HHS Assignment Tracker template."
        ) from exc
    except APIError as exc:
        if exc.response.status_code in (403, 404):
            raise GoogleSheetPermissionError(
                "Permission denied opening Google Sheet. Share the spreadsheet with "
                "your service account email (from credentials.json → client_email) "
                "and grant Editor access."
            ) from exc
        raise


def _map_status_to_template(status: Any) -> str:
    """Map Sylla Sync status values to the template's STATUS dropdown."""
    if not status or str(status).strip() == "":
        return "Not Started"
    normalized = str(status).strip().lower()
    return TEMPLATE_STATUS_OPTIONS.get(normalized, str(status).strip())


def _infer_assignment_type(title: str) -> str:
    """Guess the template TYPE column from the assignment title."""
    lowered = title.lower()
    for pattern, assignment_type in TYPE_KEYWORDS:
        if re.search(pattern, lowered):
            return assignment_type
    return "Homework"


def _split_due_datetime(due_value: Any) -> tuple[str, str]:
    """
    Split a due date into Masterlist DUE DATE (calendar) and DUE TIME strings.

    Returns:
        (date_string as MM/DD/YYYY, time_string) — empty strings if unparseable.
    """
    return format_sheet_date(due_value), format_sheet_time(due_value)


def _row_to_internal(row_values: list[Any], row_number: int) -> dict[str, Any] | None:
    """Convert a Masterlist row into Sylla Sync's internal column format."""
    # Pad to at least column H (index 7)
    cells = list(row_values) + [""] * max(0, 8 - len(row_values))

    course = str(cells[COL_CLASS - 1]).strip()
    title = str(cells[COL_ASSIGNMENT - 1]).strip()

    if not course and not title:
        return None

    due_date = cells[COL_DUE_DATE - 1]
    due_time = cells[COL_DUE_TIME - 1]

    # Column D holds the date used by DAYS UNTIL DUE; prefer it when present.
    calc_date = cells[COL_DUE_DATE_CALC - 1] if len(cells) >= COL_DUE_DATE_CALC else ""
    if str(calc_date).strip():
        due_date = calc_date

    # Combine date + time for internal storage
    combined_due = due_date
    if due_date and due_time:
        combined = pd.to_datetime(f"{due_date} {due_time}", errors="coerce")
        if pd.notna(combined):
            combined_due = combined.strftime("%Y-%m-%d %H:%M")
        else:
            combined_due = str(due_date)
    elif due_date:
        combined_due = format_internal_datetime(due_date) or str(due_date)

    return {
        "_row": row_number,
        "Course": course,
        "Assessment title": title,
        "Due Date": combined_due,
        "Estimated time dedicated to task": "",
        "Status": _map_status_to_template(cells[COL_STATUS - 1]),
        "Priority": "",
        TASK_ID_COL: "",
    }


def _internal_to_masterlist_row(record: dict[str, Any]) -> list[Any]:
    """Convert an internal record to Masterlist columns B–H."""
    due_date, due_time = _split_due_datetime(record.get("Due Date", ""))

    return [
        _map_status_to_template(record.get("Status", "")),   # B
        due_date,                                              # C (visible due date)
        due_date,                                              # D (feeds DAYS UNTIL DUE formula)
        due_time,                                              # E
        record.get("Course", ""),                              # F
        _infer_assignment_type(str(record.get("Assessment title", ""))),  # G
        record.get("Assessment title", ""),                    # H
    ]


def load_from_google_sheet() -> pd.DataFrame:
    """
    Read existing Masterlist rows into Sylla Sync's internal DataFrame format.

    Restores Task_ID from the local persistence map when possible so Calendar
    upserts stay stable across runs (Masterlist itself has no Task_ID column).

    Returns:
        DataFrame with COLUMNS schema (no _row metadata).
    """
    worksheet = _open_masterlist(_authenticate())
    all_values = worksheet.get_all_values()
    state = load_task_state()

    records: list[dict[str, Any]] = []
    for row_idx in range(DATA_START_ROW - 1, len(all_values)):
        parsed = _row_to_internal(all_values[row_idx], row_idx + 1)
        if not parsed:
            continue
        fallback = syllabus_task_id(
            parsed["Course"], parsed["Assessment title"], parsed["Due Date"]
        )
        parsed[TASK_ID_COL] = resolve_persisted_task_id(
            parsed["Course"], parsed["Assessment title"], fallback, state
        )
        records.append(parsed)

    if not records:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(records)
    return df.reindex(columns=COLUMNS).fillna("")


def _load_masterlist_with_rows(
    worksheet: gspread.Worksheet,
) -> tuple[dict[tuple[str, str], list[int]], int]:
    """
    Load existing Masterlist entries keyed by fuzzy (course, title) → row numbers.

    Returns:
        (existing_keys_to_rows, next_empty_row)
    """
    all_values = worksheet.get_all_values()
    existing: dict[tuple[str, str], list[int]] = {}
    next_empty_row = DATA_START_ROW

    for row_idx in range(DATA_START_ROW - 1, len(all_values)):
        row_number = row_idx + 1
        parsed = _row_to_internal(all_values[row_idx], row_number)
        if parsed:
            key = _normalize_key(parsed["Course"], parsed["Assessment title"])
            existing.setdefault(key, []).append(row_number)
            next_empty_row = max(next_empty_row, row_number + 1)
        elif row_idx + 1 >= DATA_START_ROW:
            cells = all_values[row_idx]
            course = cells[COL_CLASS - 1] if len(cells) >= COL_CLASS else ""
            title = cells[COL_ASSIGNMENT - 1] if len(cells) >= COL_ASSIGNMENT else ""
            if not str(course).strip() and not str(title).strip():
                next_empty_row = min(next_empty_row, row_number)

    if not existing:
        next_empty_row = DATA_START_ROW

    return existing, next_empty_row


def _clear_masterlist_row(worksheet: gspread.Worksheet, row_number: int) -> None:
    """Blank Masterlist data columns B–H for a row (leaves formula columns intact)."""
    worksheet.update(
        values=[[""] * 7],
        range_name=f"B{row_number}:H{row_number}",
        value_input_option="USER_ENTERED",
    )


def _merge_records(
    existing_df: pd.DataFrame,
    incoming_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge incoming Canvas/syllabus data with existing tracker rows.

    Preserves manual Status (and other manual columns) for matching assignments.
    """
    from sheets_module import _merge_tracker

    return _merge_tracker(existing_df, incoming_df)


def _write_masterlist_rows(
    worksheet: gspread.Worksheet,
    merged_df: pd.DataFrame,
    existing_rows: dict[tuple[str, str], list[int]],
    next_empty_row: int,
) -> int:
    """
    Write merged data to Masterlist columns B–H, ordered by days until due
    (soonest at the top). Formula columns (I+) are left untouched.

    Returns:
        Number of rows written.
    """
    from sheets_module import _sort_by_days_until_due

    if merged_df.empty:
        logger.warning("No assignment data to write to Masterlist.")
        return 0

    sorted_df = _sort_by_days_until_due(merged_df)

    updates: list[dict[str, Any]] = []
    written = 0

    for offset, record in enumerate(sorted_df.to_dict(orient="records")):
        key = _normalize_key(record["Course"], record["Assessment title"])
        if not key[0] and not key[1]:
            continue

        target_row = DATA_START_ROW + written
        row_values = _internal_to_masterlist_row(record)
        updates.append(
            {
                "range": f"B{target_row}:H{target_row}",
                "values": [row_values],
            }
        )
        written += 1

    # Clear leftover data rows below the newly written block (old unsorted / dup rows)
    clear_until = max(next_empty_row - 1, DATA_START_ROW + written - 1)
    for row_number in range(DATA_START_ROW + written, clear_until + 1):
        updates.append(
            {
                "range": f"B{row_number}:H{row_number}",
                "values": [[""] * 7],
            }
        )

    # Also clear any previously occupied rows that sit above clear_until but
    # were tracked as existing beyond the contiguous rewrite window.
    occupied = {
        row for rows in existing_rows.values() for row in rows
    }
    for row_number in occupied:
        if row_number >= DATA_START_ROW + written:
            updates.append(
                {
                    "range": f"B{row_number}:H{row_number}",
                    "values": [[""] * 7],
                }
            )

    # Deduplicate clear/write ranges — last write wins; prefer keeping data writes
    # by putting clears first then data updates... actually data is already first.
    # Collapse duplicate ranges keeping the last entry.
    by_range: dict[str, dict[str, Any]] = {}
    for item in updates:
        by_range[item["range"]] = item
    updates = list(by_range.values())

    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")

    if written:
        _apply_date_column_format(
            worksheet, DATA_START_ROW, DATA_START_ROW + written - 1
        )
        logger.info(
            "Masterlist rewritten in days-until-due order (%d row(s)).", written
        )
        print(f"      Sorted Masterlist by days until due ({written} row(s)).")

    return written


def _apply_date_column_format(
    worksheet: gspread.Worksheet, start_row: int, end_row: int
) -> None:
    """Force due-date cells (columns C and D) to display as calendar dates."""
    requests = []
    for col_index in (COL_DUE_DATE - 1, COL_DUE_DATE_CALC - 1):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "DATE",
                                "pattern": "m/d/yyyy",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

    worksheet.spreadsheet.batch_update({"requests": requests})


def push_to_google_sheet(df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge and push a DataFrame to the Masterlist worksheet.

    Only columns B–H are written. Formula columns (I+) are preserved.
    Task_ID is kept on the returned DataFrame (and local state) for Calendar.

    Returns:
        The merged DataFrame in Sylla Sync's internal format.
    """
    prepared = df.copy()
    prepared.columns = prepared.columns.str.strip()
    prepared = prepared.reindex(columns=COLUMNS).fillna("")
    # Ensure every row has a Task_ID before writing state / returning
    prepared[TASK_ID_COL] = prepared.apply(
        lambda row: ensure_task_id(row.to_dict()), axis=1
    )
    remember_task_ids(prepared)

    try:
        client = _authenticate()
        worksheet = _open_masterlist(client)

        existing_rows, next_empty_row = _load_masterlist_with_rows(worksheet)
        written = _write_masterlist_rows(
            worksheet, prepared, existing_rows, next_empty_row
        )

        logger.info(
            "Synced %d row(s) to '%s' → '%s' (columns B–H only).",
            written,
            worksheet.spreadsheet.title,
            MASTERLIST_SHEET,
        )
        return prepared

    except GoogleSheetPermissionError:
        raise
    except APIError as exc:
        if exc.response.status_code in (403, 404):
            raise GoogleSheetPermissionError(
                "Google Sheets API permission error. Ensure credentials.json is valid "
                "and the spreadsheet is shared with the service account email."
            ) from exc
        logger.error("Google Sheets API error: %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error pushing to Google Sheets: %s", exc)
        raise


def push_from_records(
    canvas_data: list[dict[str, Any]],
    syllabus_data: list[dict[str, Any]],
    existing_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Build a DataFrame from sync records, merge with existing Masterlist data, and push.

    Uses the dedupe engine (Canvas wins over syllabus) and preserves manual
    Status/Priority via Task_ID / fuzzy key matching.
    """
    from sheets_module import _incoming_from_sources, _merge_tracker

    incoming, stats = _incoming_from_sources(canvas_data, syllabus_data)
    print_dedup_stats(stats)

    if existing_df is None:
        existing_df = load_from_google_sheet()

    merged = _merge_tracker(existing_df, incoming)
    return push_to_google_sheet(merged)
