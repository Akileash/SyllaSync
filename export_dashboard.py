"""
Export assignments.xlsx to a polished interactive Sylla_Sync_Dashboard.xlsx.

Usage:
    python export_dashboard.py
    python export_dashboard.py --input assignments.xlsx --output Sylla_Sync_Dashboard.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

# Column layout (must match assignments.xlsx after skiprows=1)
COLUMNS = [
    "Course",
    "Assessment title",
    "Due Date",
    "Estimated time dedicated to task",
    "Status",
    "Priority",
]

STATUS_OPTIONS = ["Not Started", "In Progress", "Completed"]
PRIORITY_OPTIONS = ["Low", "Medium", "High"]

FONT_NAME = "Segoe UI"
TABLE_STYLE = "Table Style Medium 2"
SHEET_NAME = "Dashboard"


def load_and_clean(input_path: Path) -> pd.DataFrame:
    """Read assignments.xlsx, skipping the title row, and clean values."""
    try:
        df = pd.read_excel(
            input_path,
            sheet_name="Assessment Schedule",
            skiprows=1,
            engine="openpyxl",
        )
    except ValueError:
        df = pd.read_excel(input_path, skiprows=1, engine="openpyxl")

    df.columns = df.columns.str.strip()
    df = df.reindex(columns=COLUMNS)

    df["Due Date"] = pd.to_datetime(df["Due Date"], errors="coerce")
    df["Status"] = df["Status"].fillna("Not Started").replace("", "Not Started")
    df["Priority"] = df["Priority"].fillna("Unassigned").replace("", "Unassigned")

    for col in ("Course", "Assessment title", "Estimated time dedicated to task"):
        df[col] = df[col].fillna("").astype(str).str.strip()

    return df


def _base_format(workbook: xlsxwriter.Workbook, **extra) -> xlsxwriter.Format:
    props = {"font_name": FONT_NAME, "font_size": 11, "border": 1, "border_color": "#D0D7DE"}
    props.update(extra)
    return workbook.add_format(props)


def export_dashboard(input_path: Path, output_path: Path) -> pd.DataFrame:
    """Build the interactive Excel dashboard."""
    df = load_and_clean(input_path)
    # Sort soonest-due first (fewest days until due at the top)
    df = df.sort_values("Due Date", ascending=True, na_position="last").reset_index(
        drop=True
    )

    workbook = xlsxwriter.Workbook(str(output_path))
    worksheet = workbook.add_worksheet(SHEET_NAME)
    worksheet.hide_gridlines(2)

    # --- Formats ---
    date_fmt = _base_format(
        workbook, num_format="yyyy-mm-dd", align="center", valign="vcenter"
    )
    left_fmt = _base_format(workbook, align="left", valign="vcenter")
    center_fmt = _base_format(workbook, align="center", valign="vcenter")
    time_fmt = _base_format(workbook, align="center", valign="vcenter")

    high_priority_fmt = workbook.add_format(
        {
            "font_name": FONT_NAME,
            "font_size": 11,
            "bg_color": "#FECACA",
            "font_color": "#991B1B",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "border_color": "#D0D7DE",
        }
    )
    medium_priority_fmt = workbook.add_format(
        {
            "font_name": FONT_NAME,
            "font_size": 11,
            "bg_color": "#FEF9C3",
            "font_color": "#854D0E",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "border_color": "#D0D7DE",
        }
    )
    due_soon_fmt = workbook.add_format(
        {
            "font_name": FONT_NAME,
            "font_size": 11,
            "bg_color": "#FFEDD5",
            "font_color": "#9A3412",
            "num_format": "yyyy-mm-dd",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "border_color": "#D0D7DE",
        }
    )
    completed_row_fmt = workbook.add_format(
        {
            "font_name": FONT_NAME,
            "font_size": 11,
            "font_color": "#94A3B8",
            "font_strikeout": True,
            "border": 1,
            "border_color": "#D0D7DE",
        }
    )

    column_formats = {
        "Course": left_fmt,
        "Assessment title": left_fmt,
        "Due Date": date_fmt,
        "Estimated time dedicated to task": time_fmt,
        "Status": center_fmt,
        "Priority": center_fmt,
    }

    # --- Write header row ---
    for col_idx, column in enumerate(COLUMNS):
        worksheet.write(0, col_idx, column, center_fmt)

    # --- Write data rows ---
    for row_idx, record in enumerate(df.to_dict(orient="records"), start=1):
        for col_idx, column in enumerate(COLUMNS):
            value = record[column]
            fmt = column_formats[column]

            if column == "Due Date" and pd.notna(value):
                worksheet.write_datetime(row_idx, col_idx, value.to_pydatetime(), date_fmt)
            else:
                worksheet.write(row_idx, col_idx, value if value != "" else None, fmt)

    last_row = len(df)
    last_col = len(COLUMNS) - 1

    if last_row >= 0:
        # --- Native Excel table (banding + filter dropdowns) ---
        worksheet.add_table(
            0,
            0,
            last_row,
            last_col,
            {
                "style": TABLE_STYLE,
                "autofilter": True,
                "banded_rows": True,
            },
        )

        # --- Column widths ---
        worksheet.set_column("A:A", 15)  # Course
        worksheet.set_column("B:B", 35)  # Assessment title
        worksheet.set_column("C:C", 15)  # Due Date
        worksheet.set_column("D:D", 20)  # Estimated time
        worksheet.set_column("E:E", 15)  # Status
        worksheet.set_column("F:F", 15)  # Priority

        # --- Freeze header ---
        worksheet.freeze_panes(1, 0)

        # --- Data validation dropdowns ---
        status_col = COLUMNS.index("Status")
        priority_col = COLUMNS.index("Priority")

        worksheet.data_validation(
            1,
            status_col,
            last_row,
            status_col,
            {
                "validate": "list",
                "source": STATUS_OPTIONS,
                "input_title": "Status",
                "input_message": "Select the current status for this assessment.",
            },
        )
        worksheet.data_validation(
            1,
            priority_col,
            last_row,
            priority_col,
            {
                "validate": "list",
                "source": PRIORITY_OPTIONS,
                "input_title": "Priority",
                "input_message": "Select a priority level.",
            },
        )

        # --- Conditional formatting ---
        status_letter = xl_col_to_name(status_col)
        priority_letter = xl_col_to_name(priority_col)
        due_letter = xl_col_to_name(COLUMNS.index("Due Date"))
        table_range = f"A2:{xl_col_to_name(last_col)}{last_row + 1}"

        # Priority: High
        worksheet.conditional_format(
            1,
            priority_col,
            last_row,
            priority_col,
            {
                "type": "cell",
                "criteria": "==",
                "value": '"High"',
                "format": high_priority_fmt,
            },
        )

        # Priority: Medium
        worksheet.conditional_format(
            1,
            priority_col,
            last_row,
            priority_col,
            {
                "type": "cell",
                "criteria": "==",
                "value": '"Medium"',
                "format": medium_priority_fmt,
            },
        )

        # Due Date within 7 days
        worksheet.conditional_format(
            1,
            COLUMNS.index("Due Date"),
            last_row,
            COLUMNS.index("Due Date"),
            {
                "type": "formula",
                "criteria": (
                    f'=AND({due_letter}2>=TODAY(),{due_letter}2<=TODAY()+7)'
                ),
                "format": due_soon_fmt,
            },
        )

        # Completed: strikethrough + gray across entire row
        worksheet.conditional_format(
            table_range,
            {
                "type": "formula",
                "criteria": f'=${status_letter}2="Completed"',
                "format": completed_row_fmt,
            },
        )

    workbook.close()
    return df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a polished Sylla Sync dashboard from assignments.xlsx."
    )
    parser.add_argument(
        "--input",
        default="assignments.xlsx",
        help="Path to the source assignments workbook.",
    )
    parser.add_argument(
        "--output",
        default="Sylla_Sync_Dashboard.xlsx",
        help="Path for the exported dashboard workbook.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path.resolve()}\n"
            "Run `python main.py` first to generate assignments.xlsx."
        )

    df = export_dashboard(input_path, output_path)
    print(f"Exported {len(df)} row(s) to {output_path.resolve()}")


if __name__ == "__main__":
    main()
