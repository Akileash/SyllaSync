"""Sheet tab name and data-start-row config (fixtures only; no live Google calls)."""

from __future__ import annotations

import pandas as pd

from syllasync.config import (
    DEFAULT_SHEET_DATA_START_ROW,
    DEFAULT_SHEET_META_TAB_NAME,
    DEFAULT_SHEET_TAB_NAME,
    collect_config_issues,
    parse_sheet_data_start_row,
    parse_sheet_tab_name,
)
from syllasync import google_sheets_module as gsm


class _FakeWorksheet:
    def __init__(self, rows: list[list[object]]):
        self._rows = rows

    def get_all_values(self) -> list[list[object]]:
        return self._rows


def _sheet_row(task_id: str, course: str, title: str) -> list[str]:
    row = [""] * 13
    row[0] = task_id
    row[1] = "Submitted"
    row[2] = "11/19/2026"
    row[3] = "11/19/2026"
    row[4] = "2:00 PM"
    row[5] = course
    row[6] = "Homework"
    row[7] = title
    return row


def _record(task_id: str, course: str, title: str) -> dict[str, str]:
    return {
        "Course": course,
        "Assessment title": title,
        "Due Date": "2026-11-19 14:00",
        "Status": "Not Started",
        "Task_ID": task_id,
    }


def test_default_layout_matches_current_masterlist():
    assert DEFAULT_SHEET_TAB_NAME == "Masterlist"
    assert DEFAULT_SHEET_META_TAB_NAME == "SyllaSync_Meta"
    assert DEFAULT_SHEET_DATA_START_ROW == 11
    assert parse_sheet_tab_name("") == "Masterlist"
    assert parse_sheet_tab_name(None) == "Masterlist"
    assert parse_sheet_tab_name("   ") == "Masterlist"
    assert parse_sheet_data_start_row("") == 11
    assert parse_sheet_data_start_row(None) == 11
    assert parse_sheet_tab_name("", default="SyllaSync_Meta") == "SyllaSync_Meta"


def test_custom_tab_and_start_row_parsers():
    assert parse_sheet_tab_name("Assignments") == "Assignments"
    assert parse_sheet_tab_name("  Weekly  ") == "Weekly"
    assert parse_sheet_data_start_row("2") == 2
    assert parse_sheet_data_start_row("1") == 1
    assert parse_sheet_data_start_row("0") == 11
    assert parse_sheet_data_start_row("-3") == 11
    assert parse_sheet_data_start_row("row eleven") == 11
    assert parse_sheet_data_start_row("11") == 11


def test_default_start_row_skips_chrome_above_row_11(monkeypatch):
    chrome = [["HHS title"]] + [[""] * 8 for _ in range(9)]
    data = [_sheet_row("tid-1", "COURSE 101", "HW1")]
    monkeypatch.setattr(gsm, "DATA_START_ROW", 11)
    existing, next_empty = gsm._load_masterlist_with_rows(_FakeWorksheet(chrome + data))
    assert len(existing) == 1
    rows = next(iter(existing.values()))
    assert rows == [11]
    assert next_empty == 12


def test_custom_tab_and_start_row_load_from_row_2(monkeypatch):
    header = [
        "Task_ID",
        "Status",
        "Due date",
        "Due date",
        "Due time",
        "Course",
        "Type",
        "Title",
    ]
    rows = [
        header,
        _sheet_row("tid-1", "COURSE 101", "HW1"),
        _sheet_row("tid-2", "COURSE 102", "Quiz 1"),
    ]
    monkeypatch.setattr(gsm, "DATA_START_ROW", 2)
    monkeypatch.setattr(gsm, "MASTERLIST_SHEET", "Assignments")
    existing, next_empty = gsm._load_masterlist_with_rows(_FakeWorksheet(rows))
    found = sorted(row for rows in existing.values() for row in rows)
    assert found == [2, 3]
    assert next_empty == 4
    message = gsm.missing_tab_message()
    assert "Assignments" in message
    assert "row 2" in message
    assert "SHEET_TAB_NAME" in message
    assert "HHS" not in message


def test_write_plan_starts_at_configured_row_and_skips_header(monkeypatch):
    monkeypatch.setattr(gsm, "DATA_START_ROW", 2)
    df = pd.DataFrame([_record("tid-1", "COURSE 101", "HW1")])
    updates, written, graded = gsm._build_masterlist_updates(df, {}, 2)
    assert written == 1
    assert graded == 0
    ranges = [item["range"] for item in updates]
    assert "A2:H2" in ranges
    assert "K2:M2" in ranges
    assert all("1:" not in item and not item.endswith("1") for item in ranges)
    for item in ranges:
        # ranges look like A2:H2 — none should touch row 1
        assert "A1:" not in item and "K1:" not in item


def test_default_write_plan_starts_at_row_11(monkeypatch):
    monkeypatch.setattr(gsm, "DATA_START_ROW", 11)
    df = pd.DataFrame(
        [
            _record("tid-1", "COURSE 101", "HW1"),
            _record("tid-2", "COURSE 102", "Quiz 1"),
        ]
    )
    updates, written, _graded = gsm._build_masterlist_updates(df, {}, 11)
    assert written == 2
    ranges = [item["range"] for item in updates]
    assert "A11:H11" in ranges
    assert "A12:H12" in ranges
    assert not any(f"A{row}:" in item or f"K{row}:" in item for item in ranges for row in range(1, 11))


def test_missing_tab_message_uses_defaults():
    message = gsm.missing_tab_message("Masterlist")
    assert "Masterlist" in message
    assert "SHEET_TAB_NAME" in message
    assert "HHS" not in message
    assert "A Task_ID" in message


def test_check_flags_invalid_start_row(monkeypatch):
    monkeypatch.setenv("SHEET_DATA_START_ROW", "0")
    issues = [
        issue
        for issue in collect_config_issues(require_canvas=False)
        if issue.name == "SHEET_DATA_START_ROW"
    ]
    assert issues
    assert issues[0].level == "error"
    assert "positive integer" in issues[0].message
    assert "0" not in issues[0].message


def test_check_accepts_custom_start_row(monkeypatch):
    monkeypatch.setenv("SHEET_DATA_START_ROW", "2")
    issues = [
        issue
        for issue in collect_config_issues(require_canvas=False)
        if issue.name == "SHEET_DATA_START_ROW"
    ]
    assert issues == []
