"""Combine assignment data and write to a local Excel assessment tracker."""

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from config import EXCEL_FILE_PATH
from course_utils import normalize_course_code

logger = logging.getLogger(__name__)

# "Assignment #1: ...", "Quiz 2", "HW #3" → stable match key within a course
NUMBERED_TASK_RE = re.compile(
    r"^(assignment|quiz|exam|lab|homework|hw|project|midterm|final)\s*#?\s*(\d+)\b",
    re.IGNORECASE,
)

SHEET_NAME = "Assessment Schedule"
AUTO_COLUMNS = ["Course", "Assessment title", "Due Date"]
MANUAL_COLUMNS = [
    "Estimated time dedicated to task",
    "Status",
    "Priority",
]
COLUMNS = AUTO_COLUMNS + MANUAL_COLUMNS

TITLE = "Assessment Schedule"
TITLE_FONT = Font(name="Calibri", bold=True, size=16, color="FFFFFF")
TITLE_FILL = PatternFill("solid", fgColor="2F5496")

HEADER_FONT = Font(name="Calibri", bold=True, size=11, color="1F3864")
HEADER_FILL = PatternFill("solid", fgColor="FBF3CB")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)

BODY_FONT = Font(name="Calibri", size=11, color="333333")
ALT_ROW_FILL = PatternFill("solid", fgColor="F5F7FA")
WHITE_FILL = PatternFill("solid", fgColor="FFFFFF")

THIN_BORDER = Border(
    left=Side(style="thin", color="D0D7DE"),
    right=Side(style="thin", color="D0D7DE"),
    top=Side(style="thin", color="D0D7DE"),
    bottom=Side(style="thin", color="D0D7DE"),
)

COLUMN_WIDTHS = {
    "A": 14.0,
    "B": 28.0,
    "C": 16.0,
    "D": 22.0,
    "E": 14.0,
    "F": 12.0,
}

COLUMN_ALIGNMENTS = {
    "Course": Alignment(horizontal="left", vertical="center"),
    "Assessment title": Alignment(horizontal="left", vertical="center", wrap_text=True),
    "Due Date": Alignment(horizontal="center", vertical="center"),
    "Estimated time dedicated to task": Alignment(horizontal="center", vertical="center"),
    "Status": Alignment(horizontal="center", vertical="center"),
    "Priority": Alignment(horizontal="center", vertical="center"),
}

STATUS_OPTIONS = '"Not started,In Progress,Done"'
PRIORITY_OPTIONS = '"P 1,P 2,P 3"'


def _normalize_title_text(title: Any) -> str:
    """Lowercase title with punctuation collapsed for comparison."""
    text = str(title or "").strip().lower()
    text = re.sub(r"[^\w\s#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_key(course: Any, title: Any) -> tuple[str, str]:
    """
    Fuzzy match key so near-duplicate titles collapse.

    Examples that become the same key under ENGG 299:
      - "Assignment #1: Complete Co-op Expectations Agreement"
      - "Assignment #1: Co-op Expectations Agreement"
    """
    course_raw = str(course or "").strip()
    course_key = (normalize_course_code(course_raw) or course_raw).strip().lower()

    title_norm = _normalize_title_text(title)
    numbered = NUMBERED_TASK_RE.match(title_norm)
    if numbered:
        kind = numbered.group(1).lower()
        if kind == "hw":
            kind = "homework"
        return course_key, f"{kind}#{numbered.group(2)}"

    return course_key, title_norm


def _prefer_title(current: str, candidate: str) -> str:
    """Keep the more descriptive title when collapsing duplicates."""
    cur = str(current or "").strip()
    cand = str(candidate or "").strip()
    if not cur:
        return cand
    if not cand:
        return cur
    if len(cand) > len(cur):
        return cand
    return cur


def _collapse_by_match_key(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse rows that share the same fuzzy (course, title) match key."""
    if df.empty:
        return df.reindex(columns=[c for c in COLUMNS if c in df.columns] or list(df.columns))

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in df.iterrows():
        key = _normalize_key(row.get("Course", ""), row.get("Assessment title", ""))
        if not key[0] and not key[1]:
            continue

        if key not in best:
            best[key] = {col: row.get(col, "") for col in df.columns}
            continue

        existing = best[key]
        existing["Assessment title"] = _prefer_title(
            str(existing.get("Assessment title", "")),
            str(row.get("Assessment title", "")),
        )
        # Prefer a parseable / newer-looking due date if current is empty
        if not str(existing.get("Due Date", "")).strip() and str(row.get("Due Date", "")).strip():
            existing["Due Date"] = row.get("Due Date", "")
        for col in MANUAL_COLUMNS:
            if col in df.columns and not str(existing.get(col, "")).strip():
                existing[col] = row.get(col, "")

    if not best:
        return pd.DataFrame(columns=list(df.columns))

    return pd.DataFrame(list(best.values()), columns=list(df.columns))


def _incoming_from_sources(
    canvas_data: list[dict[str, Any]],
    syllabus_data: list[dict[str, Any]],
) -> pd.DataFrame:
    """Map Canvas/syllabus records to tracker auto-fill columns."""
    rows: list[dict[str, Any]] = []
    for item in canvas_data + syllabus_data:
        rows.append(
            {
                "Course": item.get("Course", ""),
                "Assessment title": item.get("Task", ""),
                "Due Date": item.get("Due Date", ""),
            }
        )
    return pd.DataFrame(rows, columns=AUTO_COLUMNS)


def _load_existing_tracker(path: Path) -> pd.DataFrame:
    """Load an existing tracker file, or return an empty frame."""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)

    for header_row in (1, 0):
        try:
            df = pd.read_excel(
                path, sheet_name=SHEET_NAME, engine="openpyxl", header=header_row
            )
        except ValueError:
            df = pd.read_excel(path, engine="openpyxl", header=header_row)

        if "Course" in df.columns:
            df = df.reindex(columns=COLUMNS)
            return df.fillna("")

    return pd.DataFrame(columns=COLUMNS)


def _merge_tracker(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """
    Merge synced data with the existing tracker.

    - Matching rows (fuzzy title key): update auto columns, keep manual columns.
    - New rows: add with blank manual columns.
    - Rows only in existing: keep (preserves completed/historical entries).
    - Near-duplicate titles within the same course are collapsed.
    """
    existing = _collapse_by_match_key(existing.reindex(columns=COLUMNS).fillna(""))
    incoming = _collapse_by_match_key(incoming.reindex(columns=AUTO_COLUMNS).fillna(""))

    manual_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    existing_title_by_key: dict[tuple[str, str], str] = {}
    for _, row in existing.iterrows():
        key = _normalize_key(row["Course"], row["Assessment title"])
        if not key[0] and not key[1]:
            continue
        manual_by_key[key] = {col: row.get(col, "") for col in MANUAL_COLUMNS}
        existing_title_by_key[key] = str(row.get("Assessment title", ""))

    merged_rows: list[dict[str, Any]] = []
    incoming_keys: set[tuple[str, str]] = set()

    for _, row in incoming.iterrows():
        key = _normalize_key(row["Course"], row["Assessment title"])
        if not key[0] and not key[1]:
            continue

        incoming_keys.add(key)
        manual = manual_by_key.get(key, {col: "" for col in MANUAL_COLUMNS})
        title = _prefer_title(
            existing_title_by_key.get(key, ""),
            str(row.get("Assessment title", "")),
        )
        merged_rows.append(
            {
                "Course": row["Course"],
                "Assessment title": title,
                "Due Date": row["Due Date"],
                **manual,
            }
        )

    for _, row in existing.iterrows():
        key = _normalize_key(row["Course"], row["Assessment title"])
        if not key[0] and not key[1]:
            continue
        if key in incoming_keys:
            continue
        merged_rows.append({col: row.get(col, "") for col in COLUMNS})

    if not merged_rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(merged_rows, columns=COLUMNS)
    df = _collapse_by_match_key(df)
    df["_sort_date"] = pd.to_datetime(df["Due Date"], errors="coerce")
    df = df.sort_values("_sort_date", na_position="last").drop(columns="_sort_date")
    return df.reset_index(drop=True)


def _apply_title_row(ws) -> int:
    """Add a merged title banner. Returns the header row index."""
    last_col = get_column_letter(len(COLUMNS))
    ws.merge_cells(f"A1:{last_col}1")
    title_cell = ws["A1"]
    title_cell.value = TITLE
    title_cell.font = TITLE_FONT
    title_cell.fill = TITLE_FILL
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 34
    return 2


def _apply_header_row(ws, header_row: int) -> None:
    """Style the column header row."""
    ws.row_dimensions[header_row].height = 36
    for col_idx, header in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


def _apply_body_styles(ws, header_row: int, data_row_count: int) -> None:
    """Apply zebra striping, borders, and alignment to data rows."""
    first_data_row = header_row + 1
    last_data_row = header_row + data_row_count

    for row_idx in range(first_data_row, last_data_row + 1):
        is_alt = (row_idx - first_data_row) % 2 == 1
        row_fill = ALT_ROW_FILL if is_alt else WHITE_FILL
        ws.row_dimensions[row_idx].height = 22

        for col_idx, column in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = BODY_FONT
            cell.fill = row_fill
            cell.border = THIN_BORDER
            cell.alignment = COLUMN_ALIGNMENTS.get(
                column, Alignment(vertical="center")
            )

            if column == "Due Date" and cell.value:
                cell.number_format = "ddd, mmm d, yyyy"


def _apply_validations(ws, header_row: int, data_row_count: int) -> None:
    """Add dropdown lists for Status and Priority."""
    if data_row_count == 0:
        return

    first_row = header_row + 1
    last_row = max(header_row + data_row_count, header_row + 50)

    status_col = get_column_letter(COLUMNS.index("Status") + 1)
    priority_col = get_column_letter(COLUMNS.index("Priority") + 1)

    status_dv = DataValidation(type="list", formula1=STATUS_OPTIONS, allow_blank=True)
    status_dv.error = "Pick a status from the list."
    status_dv.prompt = "Select status"
    ws.add_data_validation(status_dv)
    status_dv.add(f"{status_col}{first_row}:{status_col}{last_row}")

    priority_dv = DataValidation(type="list", formula1=PRIORITY_OPTIONS, allow_blank=True)
    priority_dv.error = "Pick a priority from the list."
    priority_dv.prompt = "Select priority"
    ws.add_data_validation(priority_dv)
    priority_dv.add(f"{priority_col}{first_row}:{priority_col}{last_row}")


def _apply_conditional_formatting(ws, header_row: int, data_row_count: int) -> None:
    """Highlight overdue dates and color-code status values."""
    if data_row_count == 0:
        return

    first_row = header_row + 1
    last_row = header_row + data_row_count
    due_col = get_column_letter(COLUMNS.index("Due Date") + 1)
    status_col = get_column_letter(COLUMNS.index("Status") + 1)

    overdue_fill = PatternFill("solid", fgColor="FCE4D6")
    overdue_font = Font(name="Calibri", size=11, color="9C0006", bold=True)
    ws.conditional_formatting.add(
        f"{due_col}{first_row}:{due_col}{last_row}",
        FormulaRule(
            formula=[f'AND(${due_col}{first_row}<>"",${due_col}{first_row}<TODAY())'],
            fill=overdue_fill,
            font=overdue_font,
        ),
    )

    done_fill = PatternFill("solid", fgColor="E2EFDA")
    done_font = Font(name="Calibri", size=11, color="375623")
    ws.conditional_formatting.add(
        f"A{first_row}:{get_column_letter(len(COLUMNS))}{last_row}",
        FormulaRule(
            formula=[f'${status_col}{first_row}="Done"'],
            fill=done_fill,
            font=done_font,
        ),
    )


def _write_tracker(path: Path, df: pd.DataFrame) -> None:
    """Write a styled assessment tracker workbook."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.sheet_view.showGridLines = False

    header_row = _apply_title_row(ws)
    _apply_header_row(ws, header_row)

    data_start_row = header_row + 1
    for row_idx, record in enumerate(df.to_dict(orient="records"), start=data_start_row):
        for col_idx, column in enumerate(COLUMNS, start=1):
            value = record.get(column, "")
            if column == "Due Date" and value:
                parsed = pd.to_datetime(value, errors="coerce")
                value = parsed.to_pydatetime() if pd.notna(parsed) else value
            ws.cell(row=row_idx, column=col_idx, value=value or None)

    data_row_count = len(df)
    _apply_body_styles(ws, header_row, data_row_count)
    _apply_validations(ws, header_row, data_row_count)
    _apply_conditional_formatting(ws, header_row, data_row_count)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1).coordinate
    ws.auto_filter.ref = (
        f"A{header_row}:{get_column_letter(len(COLUMNS))}"
        f"{max(header_row + data_row_count, header_row)}"
    )

    for letter, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[letter].width = width

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def update_sheet(
    canvas_data: list[dict[str, Any]],
    syllabus_data: list[dict[str, Any]],
) -> pd.DataFrame:
    """
    Combine data sources, merge with existing tracker, and write Excel file.

    Returns:
        The combined DataFrame that was written to the file.
    """
    output_path = Path(EXCEL_FILE_PATH)
    existing = _load_existing_tracker(output_path)
    incoming = _incoming_from_sources(canvas_data, syllabus_data)

    if incoming.empty and existing.empty:
        logger.warning("No assignment data to write.")

    df = _merge_tracker(existing, incoming)

    try:
        _write_tracker(output_path, df)
        logger.info("Wrote %d row(s) to %s.", len(df), output_path.resolve())
    except OSError as exc:
        logger.error("Failed to write Excel file: %s", exc)
        raise

    return df
