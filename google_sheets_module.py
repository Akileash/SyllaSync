"""
Push assignment data to the HHS Assignment Tracker Masterlist in Google Sheets.

Requires:
  - GOOGLE_SHEET_ID in .env / local.env
  - credentials.json service-account key in the project root
  - Sheet shared with the service account email (Editor access)

Integration targets the **Masterlist** tab. Columns I onward (formulas,
charts, calendars) are never modified. Column A stores Sylla Sync Task_ID.
A companion SyllaSync_Meta sheet mirrors Task_ID for cross-machine hydration.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import gspread
import pandas as pd
from gspread.exceptions import APIError, SpreadsheetNotFound

from config import BASE_DIR, GOOGLE_SHEET_ID
from date_utils import (
    apply_math209_monday_due,
    format_internal_datetime,
    format_sheet_date,
    format_sheet_time,
    split_title_and_due,
)
from dedupe_module import (
    TASK_ID_COL,
    ensure_task_id,
    hydrate_state_from_rows,
    is_droppable_placeholder,
    load_task_state,
    print_dedup_stats,
    remember_task_ids,
    resolve_persisted_task_id,
    syllabus_task_id,
)
from retry_utils import with_retries
from sheets_module import incoming_from_sources, merge_tracker, normalize_key, sort_by_days_until_due
from vocab import sheet_status

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
COLUMNS = DISPLAY_COLUMNS + [TASK_ID_COL, "Is Draft"]

# HHS Assignment Tracker — Masterlist layout
MASTERLIST_SHEET = "Masterlist"
META_SHEET = "SyllaSync_Meta"
DATA_START_ROW = 11

# 1-based column indices on Masterlist (A=1)
COL_TASK_ID = 1  # A — Sylla Sync Task_ID (dedicated persistence column)
COL_STATUS = 2  # B
COL_DUE_DATE = 3  # C
COL_DUE_DATE_CALC = 4  # D
COL_DUE_TIME = 5  # E
COL_CLASS = 6  # F
COL_TYPE = 7  # G
COL_ASSIGNMENT = 8  # H

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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


@with_retries(label="sheets.authenticate")
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


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    try:
        return client.open_by_key(GOOGLE_SHEET_ID)
    except SpreadsheetNotFound as exc:
        raise GoogleSheetPermissionError(
            "Spreadsheet not found. Verify GOOGLE_SHEET_ID and ensure the sheet "
            "is shared with your service account email as Editor."
        ) from exc
    except APIError as exc:
        if exc.response.status_code in (403, 404):
            raise GoogleSheetPermissionError(
                "Permission denied opening Google Sheet. Share the spreadsheet with "
                "your service account email (from credentials.json client_email) "
                "and grant Editor access."
            ) from exc
        raise


def _open_masterlist(client: gspread.Client) -> gspread.Worksheet:
    """Open the Masterlist worksheet."""
    spreadsheet = _open_spreadsheet(client)
    try:
        return spreadsheet.worksheet(MASTERLIST_SHEET)
    except gspread.WorksheetNotFound as exc:
        raise GoogleSheetPermissionError(
            f"Worksheet '{MASTERLIST_SHEET}' not found. "
            "Ensure you are using the HHS Assignment Tracker template."
        ) from exc


def _ensure_meta_sheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Create or open the Task_ID meta sheet (does not touch Masterlist formulas)."""
    try:
        return spreadsheet.worksheet(META_SHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=META_SHEET, rows=2000, cols=4)
        ws.update(
            values=[["Task_ID", "Course", "Assessment title", "Due Date"]],
            range_name="A1:D1",
        )
        return ws


def _map_status_to_template(status: Any) -> str:
    """Map Sylla Sync status values to the template's STATUS dropdown."""
    return sheet_status(status)


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

    combined_due = due_date
    if due_date and due_time:
        combined = pd.to_datetime(f"{due_date} {due_time}", errors="coerce")
        if pd.notna(combined):
            combined_due = combined.strftime("%Y-%m-%d %H:%M")
        else:
            combined_due = str(due_date)
    elif due_date:
        combined_due = format_internal_datetime(due_date) or str(due_date)

    # Clean titles that still contain "Due date …" from older syncs
    title, combined_due = split_title_and_due(title, combined_due)
    combined_due = apply_math209_monday_due(course, title, combined_due)

    return {
        "_row": row_number,
        "Course": course,
        "Assessment title": title,
        "Due Date": combined_due,
        "Estimated time dedicated to task": "",
        "Status": _map_status_to_template(cells[COL_STATUS - 1]),
        "Priority": "",
        TASK_ID_COL: str(cells[COL_TASK_ID - 1]).strip() if cells else "",
        "Is Draft": False,
    }


def _internal_to_masterlist_row(record: dict[str, Any]) -> list[Any] | None:
    """
    Convert an internal record to Masterlist columns A–H (Task_ID + B–H).

    Column C = visible DUE DATE, column D = same calendar date for the
    template's DAYS UNTIL DUE formula (I+). They are intentionally identical.
    Column E holds the time (e.g. 11:45 PM) when present.
    """
    title = str(record.get("Assessment title", "") or "")
    due_raw = record.get("Due Date", "")
    clean_title, due_raw = split_title_and_due(title, due_raw)
    due_raw = apply_math209_monday_due(record.get("Course", ""), clean_title, due_raw)
    if is_droppable_placeholder(clean_title, due_raw):
        # Signal caller to skip this row entirely
        return None
    due_date, due_time = _split_due_datetime(due_raw)
    task_id = str(record.get(TASK_ID_COL) or ensure_task_id(record)).strip()

    return [
        task_id,  # A
        _map_status_to_template(record.get("Status", "")),  # B
        due_date,  # C — visible due date
        due_date,  # D — same date; feeds DAYS UNTIL DUE formulas
        due_time,  # E — due time only
        record.get("Course", ""),  # F
        _infer_assignment_type(clean_title),  # G
        clean_title,  # H — never keep embedded "Due date …" in the title
    ]


@with_retries(label="sheets.get_all_values")
def _get_all_values(worksheet: gspread.Worksheet) -> list[list[Any]]:
    return worksheet.get_all_values()


def _load_meta_task_map(spreadsheet: gspread.Spreadsheet) -> dict[str, str]:
    """Map fuzzy (course||title) -> Task_ID from Meta sheet."""
    try:
        meta = spreadsheet.worksheet(META_SHEET)
    except gspread.WorksheetNotFound:
        return {}
    values = _get_all_values(meta)
    mapping: dict[str, str] = {}
    for row in values[1:]:
        if len(row) < 3:
            continue
        task_id, course, title = str(row[0]).strip(), str(row[1]).strip(), str(row[2]).strip()
        if task_id and (course or title):
            key = f"{normalize_key(course, title)[0]}||{normalize_key(course, title)[1]}"
            # Prefer canvas_ over syllabus_ on collision
            existing = mapping.get(key, "")
            if existing.startswith("canvas_") and not task_id.startswith("canvas_"):
                continue
            mapping[key] = task_id
    return mapping


def load_from_google_sheet() -> pd.DataFrame:
    """
    Read existing Masterlist rows into Sylla Sync's internal DataFrame format.

    Hydrates Task_ID from column A, Meta sheet, then local state — rebuilding
    local JSON when wiped so identity survives across machines.
    """
    client = _authenticate()
    spreadsheet = _open_spreadsheet(client)
    worksheet = spreadsheet.worksheet(MASTERLIST_SHEET)
    all_values = _get_all_values(worksheet)
    meta_map = _load_meta_task_map(spreadsheet)
    state = load_task_state()

    records: list[dict[str, Any]] = []
    for row_idx in range(DATA_START_ROW - 1, len(all_values)):
        parsed = _row_to_internal(all_values[row_idx], row_idx + 1)
        if not parsed:
            continue

        sheet_tid = str(parsed.get(TASK_ID_COL) or "").strip()
        key = normalize_key(parsed["Course"], parsed["Assessment title"])
        meta_tid = meta_map.get(f"{key[0]}||{key[1]}", "")
        fallback = syllabus_task_id(
            parsed["Course"], parsed["Assessment title"], parsed["Due Date"]
        )
        preferred = sheet_tid or meta_tid or fallback
        parsed[TASK_ID_COL] = resolve_persisted_task_id(
            parsed["Course"], parsed["Assessment title"], preferred, state
        )
        records.append(parsed)

    # Rebuild local cache from sheet so wiped machines recover identity
    if records:
        hydrate_state_from_rows(records, state)

    if not records:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(records)
    return df.reindex(columns=COLUMNS).fillna("")


def _load_masterlist_with_rows(
    worksheet: gspread.Worksheet,
) -> tuple[dict[tuple[str, str], list[int]], int]:
    """
    Load existing Masterlist entries keyed by fuzzy (course, title) -> row numbers.

    Returns:
        (existing_keys_to_rows, next_empty_row)
    """
    all_values = _get_all_values(worksheet)
    existing: dict[tuple[str, str], list[int]] = {}
    next_empty_row = DATA_START_ROW

    for row_idx in range(DATA_START_ROW - 1, len(all_values)):
        row_number = row_idx + 1
        parsed = _row_to_internal(all_values[row_idx], row_number)
        if parsed:
            key = normalize_key(parsed["Course"], parsed["Assessment title"])
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


def preview_sheets_diff(
    existing_df: pd.DataFrame,
    merged_df: pd.DataFrame,
) -> dict[str, Any]:
    """Compute a dry-run summary of Masterlist changes."""
    old_ids = set(
        str(x).strip()
        for x in existing_df.get(TASK_ID_COL, pd.Series(dtype=str)).tolist()
        if str(x).strip()
    )
    new_ids = set(
        str(x).strip()
        for x in merged_df.get(TASK_ID_COL, pd.Series(dtype=str)).tolist()
        if str(x).strip()
    )
    return {
        "rows_existing": len(existing_df),
        "rows_merged": len(merged_df),
        "task_ids_added": sorted(new_ids - old_ids),
        "task_ids_removed": sorted(old_ids - new_ids),
        "task_ids_kept": sorted(old_ids & new_ids),
    }


@with_retries(label="sheets.batch_update")
def _batch_update(worksheet: gspread.Worksheet, updates: list[dict[str, Any]]) -> None:
    worksheet.batch_update(updates, value_input_option="USER_ENTERED")


def _write_meta_sheet(
    spreadsheet: gspread.Spreadsheet,
    merged_df: pd.DataFrame,
    *,
    dry_run: bool = False,
) -> None:
    """Persist Task_ID map to Meta sheet for cross-machine hydration."""
    if dry_run:
        logger.info("[dry-run] Would rewrite %s with %d Task_ID row(s).", META_SHEET, len(merged_df))
        return
    meta = _ensure_meta_sheet(spreadsheet)
    rows = [["Task_ID", "Course", "Assessment title", "Due Date"]]
    for record in merged_df.to_dict(orient="records"):
        rows.append(
            [
                str(record.get(TASK_ID_COL) or ""),
                str(record.get("Course") or ""),
                str(record.get("Assessment title") or ""),
                str(record.get("Due Date") or ""),
            ]
        )
    # Clear + rewrite (Meta has no user formulas)
    meta.clear()
    meta.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")


def _write_masterlist_rows(
    worksheet: gspread.Worksheet,
    merged_df: pd.DataFrame,
    existing_rows: dict[tuple[str, str], list[int]],
    next_empty_row: int,
    *,
    dry_run: bool = False,
) -> int:
    """
    Write merged data to Masterlist columns A–H.
    Active rows are ordered soonest-due first; Submitted/Complete sink to the bottom.
    Formula columns (I+) are left untouched.
    """
    if merged_df.empty:
        logger.warning("No assignment data to write to Masterlist.")
        return 0

    sorted_df = sort_by_days_until_due(merged_df)

    updates: list[dict[str, Any]] = []
    written = 0

    for record in sorted_df.to_dict(orient="records"):
        key = normalize_key(record["Course"], record["Assessment title"])
        if not key[0] and not key[1]:
            continue

        target_row = DATA_START_ROW + written
        row_values = _internal_to_masterlist_row(record)
        if row_values is None:
            continue
        updates.append(
            {
                "range": f"A{target_row}:H{target_row}",
                "values": [row_values],
            }
        )
        written += 1

    clear_until = max(next_empty_row - 1, DATA_START_ROW + written - 1)
    for row_number in range(DATA_START_ROW + written, clear_until + 1):
        updates.append(
            {
                "range": f"A{row_number}:H{row_number}",
                "values": [[""] * 8],
            }
        )

    occupied = {row for rows in existing_rows.values() for row in rows}
    for row_number in occupied:
        if row_number >= DATA_START_ROW + written:
            updates.append(
                {
                    "range": f"A{row_number}:H{row_number}",
                    "values": [[""] * 8],
                }
            )

    by_range: dict[str, dict[str, Any]] = {}
    for item in updates:
        by_range[item["range"]] = item
    updates = list(by_range.values())

    if dry_run:
        logger.info("[dry-run] Would write %d Masterlist row(s) (A-H).", written)
        print(f"      [dry-run] Would write {written} Masterlist row(s).")
        return written

    if updates:
        _batch_update(worksheet, updates)

    if written:
        _apply_date_column_format(
            worksheet, DATA_START_ROW, DATA_START_ROW + written - 1
        )
        logger.info(
            "Masterlist rewritten (active soonest-first; submitted at bottom) (%d row(s)).",
            written,
        )
        print(
            f"      Sorted Masterlist: active soonest-first, "
            f"submitted at bottom ({written} row(s))."
        )

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


def push_to_google_sheet(
    df: pd.DataFrame,
    *,
    dry_run: bool = False,
) -> pd.DataFrame:
    """
    Merge and push a DataFrame to the Masterlist worksheet.

    Writes columns A–H (Task_ID in A). Formula columns (I+) are preserved.
    """
    prepared = df.copy()
    prepared.columns = prepared.columns.str.strip()
    prepared = prepared.reindex(columns=COLUMNS).fillna("")
    prepared[TASK_ID_COL] = prepared.apply(
        lambda row: ensure_task_id(row.to_dict()), axis=1
    )
    remember_task_ids(prepared)

    try:
        client = _authenticate()
        spreadsheet = _open_spreadsheet(client)
        worksheet = spreadsheet.worksheet(MASTERLIST_SHEET)

        existing_rows, next_empty_row = _load_masterlist_with_rows(worksheet)
        written = _write_masterlist_rows(
            worksheet,
            prepared,
            existing_rows,
            next_empty_row,
            dry_run=dry_run,
        )
        _write_meta_sheet(spreadsheet, prepared, dry_run=dry_run)

        logger.info(
            "%s %d row(s) to '%s' / '%s' (columns A-H).",
            "Would sync" if dry_run else "Synced",
            written,
            spreadsheet.title,
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
    *,
    dry_run: bool = False,
) -> pd.DataFrame:
    """
    Build a DataFrame from sync records, merge with existing Masterlist data, and push.

    Uses the dedupe engine (Canvas wins over syllabus) and preserves manual
    Status/Priority via Task_ID / fuzzy key matching.
    """
    incoming, stats = incoming_from_sources(canvas_data, syllabus_data)
    print_dedup_stats(stats)

    if existing_df is None:
        existing_df = load_from_google_sheet()

    merged = merge_tracker(existing_df, incoming)

    if dry_run:
        diff = preview_sheets_diff(existing_df, merged)
        print(
            f"      [dry-run] Sheets diff: "
            f"+{len(diff['task_ids_added'])} "
            f"-{len(diff['task_ids_removed'])} "
            f"={len(diff['task_ids_kept'])} kept"
        )
        for tid in diff["task_ids_added"][:10]:
            print(f"        + {tid}")
        for tid in diff["task_ids_removed"][:10]:
            print(f"        - {tid}")

    return push_to_google_sheet(merged, dry_run=dry_run)
