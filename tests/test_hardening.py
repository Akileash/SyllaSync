"""Unit tests for Sylla Sync hardening (dedupe, dates, schedule, calendar)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from canvas_module import section_allowed
from date_utils import (
    calendar_event_bounds,
    due_window_bounds,
    has_explicit_time,
    normalize_calendar_date,
    parse_due_datetime,
)
from dedupe_module import (
    TASK_ID_COL,
    canvas_task_id,
    dedupe_cross_source,
    fuzzy_title_key,
    is_phantom_task,
    promote_task_id,
    resolve_persisted_task_id,
    syllabus_task_id,
)
from google_calendar_module import compute_obsolete_event_ids
from vocab import calendar_color, calendar_summary_prefix, normalize_status, sheet_status


# ---------------------------------------------------------------------------
# Deduplication & ID promotion
# ---------------------------------------------------------------------------


def test_fuzzy_title_variants_match():
    assert fuzzy_title_key("Midterm Exam") == fuzzy_title_key("Midterm 1")
    assert fuzzy_title_key("Assignment 2 - Written") == fuzzy_title_key("A2")
    assert fuzzy_title_key("Homework 3") == fuzzy_title_key("HW 3")


def test_syllabus_to_canvas_promotion(tmp_path, monkeypatch):
    state_file = tmp_path / ".syllasync_task_state.json"
    monkeypatch.setattr("dedupe_module.STATE_PATH", state_file)

    canvas = [
        {
            "Course": "MATH 201",
            "Task": "Assignment 1",
            "Due Date": "2026-09-15 23:59",
            "Canvas ID": 4242,
        }
    ]
    syllabus = [
        {
            "Course": "MATH 201",
            "Task": "A1",
            "Due Date": "2026-09-15 23:59",
        }
    ]
    kept, stats = dedupe_cross_source(canvas, syllabus)
    assert stats.kept == 1
    assert stats.discarded >= 1
    assert kept[0][TASK_ID_COL] == canvas_task_id(4242)
    assert kept[0][TASK_ID_COL].startswith("canvas_")


def test_resolve_persisted_promotes_syllabus_to_canvas():
    state = {
        "by_match_key": {
            "math 201||assignment#1": {"task_id": "syllabus_math_201_a1_2026-09-15"},
        },
        "aliases": {},
    }
    preferred = canvas_task_id(99)
    result = resolve_persisted_task_id("MATH 201", "Assignment 1", preferred, state)
    assert result == preferred
    assert state["aliases"]["syllabus_math_201_a1_2026-09-15"] == preferred


def test_promote_task_id_rewrites_map():
    state = {
        "by_match_key": {"k": {"task_id": "syllabus_old"}},
        "aliases": {},
    }
    promote_task_id("syllabus_old", "canvas_1", state)
    assert state["aliases"]["syllabus_old"] == "canvas_1"
    assert state["by_match_key"]["k"]["task_id"] == "canvas_1"


def test_phantom_tasks_flagged():
    assert is_phantom_task("Assignments", "") is True
    assert is_phantom_task("Final Exam", "TBD") is True
    assert is_phantom_task("Midterm", None) is True
    assert is_phantom_task("Assignment 1", "2026-09-20") is False
    # Bare "Labs" stays phantom even when a nearby PDF date was attached
    assert is_phantom_task("Labs", "2026-09-08") is True
    assert is_phantom_task("Lab 3", "2026-09-08") is False


def test_dedupe_drops_category_phantoms(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json"
    )
    kept, stats = dedupe_cross_source(
        [],
        [
            {"Course": "MATH 201", "Task": "Labs", "Due Date": "2026-09-08"},
            {"Course": "ECE 210", "Task": "Quizzes", "Due Date": "TBD"},
        ],
    )
    assert kept == []
    assert stats.discarded_reasons.get("syllabus_category_phantom", 0) >= 2


# ---------------------------------------------------------------------------
# Date / timezone
# ---------------------------------------------------------------------------


def test_normalize_calendar_date():
    assert normalize_calendar_date("2026-09-15") == "2026-09-15"
    assert normalize_calendar_date("TBD") == ""
    assert normalize_calendar_date("Wed") == ""


def test_has_explicit_time():
    assert has_explicit_time("2026-09-15 23:59") is True
    assert has_explicit_time("2026-09-15 5:00 PM") is True
    assert has_explicit_time("2026-09-15") is False


def test_timed_calendar_bounds_not_all_day(monkeypatch):
    monkeypatch.setenv("LOCAL_TIMEZONE", "America/Edmonton")
    # Reload isn't needed — local_tz reads config.LOCAL_TIMEZONE at call time
    # but config already loaded; calendar_event_bounds uses local_tz() which
    # reads LOCAL_TIMEZONE from config module — patch that.
    import date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    bounds = calendar_event_bounds("2026-09-15 23:59")
    assert bounds is not None
    assert bounds["all_day"] is False
    assert "dateTime" in bounds["start"]
    assert "dateTime" in bounds["end"]
    assert "timeZone" in bounds["start"]


def test_all_day_calendar_bounds():
    bounds = calendar_event_bounds("2026-09-15")
    assert bounds is not None
    assert bounds["all_day"] is True
    assert bounds["start"]["date"] == "2026-09-15"
    assert bounds["end"]["date"] == "2026-09-16"


def test_due_window_includes_today_through_end_of_day(monkeypatch):
    import date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    tz = ZoneInfo("America/Edmonton")
    # 00:30 just after midnight
    now = datetime(2026, 9, 15, 0, 30, tzinfo=tz)
    start, end = due_window_bounds(now=now, lookahead_days=0)
    assert start.hour == 0 and start.minute == 0
    assert end.hour == 23 and end.minute == 59
    # Date-only "today" at midnight still inside window
    due = parse_due_datetime("2026-09-15")
    assert due is not None
    assert start <= due.replace(tzinfo=tz) <= end or start <= due <= end


# ---------------------------------------------------------------------------
# Schedule section filtering
# ---------------------------------------------------------------------------


def test_section_filter_allows_matching_lab():
    schedule = {
        "sections": [
            {"course": "MATH 209", "type": "LAB", "section": "EL05"},
            {"course": "MATH 209", "type": "LEC", "section": "A1"},
        ]
    }
    assert section_allowed("MATH 209 LAB EL05", schedule) is True
    assert section_allowed("MATH 209 LEC A1", schedule) is True


def test_section_filter_rejects_other_lab():
    schedule = {
        "sections": [
            {"course": "MATH 209", "type": "LAB", "section": "EL05"},
        ]
    }
    assert section_allowed("MATH 209 LAB EL99", schedule) is False


def test_section_filter_allows_parent_shell_without_section_letters():
    schedule = {
        "sections": [
            {"course": "ECE 210", "type": "LEC", "section": "A1"},
        ]
    }
    # Parent course shells often omit LEC/LAB markers
    assert section_allowed("ECE 210 - Fall 2026", schedule) is True


def test_section_filter_noop_when_schedule_empty():
    assert section_allowed("ANY COURSE LAB Z9", {"sections": []}) is True


# ---------------------------------------------------------------------------
# Calendar obsolete-event diff
# ---------------------------------------------------------------------------


def test_compute_obsolete_event_ids():
    existing = [
        {"id": "e1", "task_id": "canvas_1", "fuzzy_key": "a"},
        {"id": "e2", "task_id": "canvas_2", "fuzzy_key": "b"},
        {"id": "e3", "task_id": "syllabus_old", "fuzzy_key": "c"},
        {"id": "e4", "task_id": "", "fuzzy_key": "orphan"},
    ]
    active = {"canvas_1", "canvas_2"}
    keep = {"e1", "e2"}
    obsolete = compute_obsolete_event_ids(existing, active, keep)
    assert "e3" in obsolete
    assert "e4" in obsolete
    assert "e1" not in obsolete
    assert "e2" not in obsolete


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_vocab_status_sheet_mapping():
    assert sheet_status("not started") == "Not Started"
    assert sheet_status("submitted") == "Submitted"
    assert normalize_status("done").value == "COMPLETE"


def test_calendar_status_prefix_and_color():
    assert calendar_summary_prefix("Complete").startswith("[DONE]")
    assert calendar_summary_prefix("Submitted").startswith("[SUBMITTED]")
    assert calendar_color("Complete", "High") == "2"
    assert calendar_color("Not Started", "High") == "11"


def test_discord_skips_completed_status():
    from discord_module import _is_finished_status

    assert _is_finished_status("Complete") is True
    assert _is_finished_status("Completed") is True
    assert _is_finished_status("Done") is True
    assert _is_finished_status("Submitted") is True
    assert _is_finished_status("Graded") is True
    assert _is_finished_status("Not Started") is False
    assert _is_finished_status("In Progress") is False


def test_canvas_utc_due_converts_to_local(monkeypatch):
    import date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    # 2026-09-17 05:59 UTC == 2026-09-16 23:59 MDT
    assert (
        date_utils.format_internal_datetime("2026-09-17T05:59:00Z")
        == "2026-09-16 23:59"
    )


def test_assignment_one_fuzzy_key():
    from dedupe_module import fuzzy_match_key, fuzzy_title_key

    assert fuzzy_title_key("Assignment One - 2026 Co-op Agreement") == "assignment#1"
    assert fuzzy_title_key("Assignment 1") == "assignment#1"
    assert fuzzy_match_key("ENGG 299", "Assignment One - 2026 Co-op") == fuzzy_match_key(
        "ENGG 299", "Assignment 1"
    )


def test_split_title_and_due_moves_date_out_of_title(monkeypatch):
    import date_utils

    monkeypatch.setattr("config.TERM_YEAR", 2026)

    title, due = date_utils.split_title_and_due(
        "Online Assignment 2- Due date Oct 6, 11:45 PM", ""
    )
    assert title == "Online Assignment 2"
    assert due.startswith("2026-10-06")
    assert "23:45" in due

    title2, due2 = date_utils.split_title_and_due(
        "Online Assignment 1- Due date Monday Sept 22", ""
    )
    assert title2 == "Online Assignment 1"
    assert due2.startswith("2026-09-22")

    # Multiline Canvas-style title
    title_nl, due_nl = date_utils.split_title_and_due(
        "Online Assignment 4\nDue date Nov 3, 11:45 PM", ""
    )
    assert title_nl == "Online Assignment 4"
    assert due_nl.startswith("2026-11-03")

    # Canvas due wins over title fragment; title still cleaned
    title3, due3 = date_utils.split_title_and_due(
        "Online Assignment 3- Due date Oct 20, 11:45 PM",
        "2026-10-20 23:45",
    )
    assert title3 == "Online Assignment 3"
    assert due3 == "2026-10-20 23:45"

    # "No due date" placeholder yields to title parse
    title4, due4 = date_utils.split_title_and_due(
        "Online Assignment 5- Due date Nov 17, 11:45 PM",
        "No due date",
    )
    assert title4 == "Online Assignment 5"
    assert due4.startswith("2026-11-17")


def test_mth_vs_math_201w_normalize_same():
    from course_utils import courses_equivalent, normalize_course_code, prefer_course_label
    from dedupe_module import fuzzy_match_key

    assert normalize_course_code("MTH 201") == "MATH 201"
    assert normalize_course_code("Math 201W") == "MATH 201"
    assert normalize_course_code("MATH 201") == "MATH 201"
    assert courses_equivalent("MTH 201", "MATH 201W")
    assert prefer_course_label("MTH 201", "MATH 201W") == "MATH 201"
    assert fuzzy_match_key("MTH 201", "Assignment 1") == fuzzy_match_key(
        "MATH 201W", "Assignment 1"
    )


def test_merge_drops_stale_labs_placeholder():
    from sheets_module import merge_tracker

    existing = pd.DataFrame(
        [
            {
                "Course": "MATH 201",
                "Assessment title": "Labs",
                "Due Date": "2026-09-08",
                "Estimated time dedicated to task": "",
                "Status": "Not Started",
                "Priority": "",
                TASK_ID_COL: "syllabus_labs",
                "Is Draft": False,
            },
            {
                "Course": "MATH 201",
                "Assessment title": "Online Assignment 1- Due date Monday Sept 22",
                "Due Date": "",
                "Estimated time dedicated to task": "",
                "Status": "Not Started",
                "Priority": "",
                TASK_ID_COL: "syllabus_oa1_dirty",
                "Is Draft": False,
            },
        ]
    )
    incoming = pd.DataFrame(
        [
            {
                "Course": "MATH 201",
                "Assessment title": "Online Assignment 1",
                "Due Date": "2026-09-22",
                TASK_ID_COL: "canvas_1",
                "Is Draft": False,
                "Source": "Canvas",
            }
        ]
    )
    merged = merge_tracker(existing, incoming)
    titles = [str(t).lower() for t in merged["Assessment title"].tolist()]
    assert "labs" not in titles
    assert len(merged) == 1
    assert merged.iloc[0]["Assessment title"] == "Online Assignment 1"
    assert str(merged.iloc[0]["Due Date"]).startswith("2026-09-22")


def test_cross_source_dedupe_collapses_course_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json"
    )
    canvas = [
        {
            "Course": "MATH 201W",
            "Task": "Assignment 2",
            "Due Date": "2026-09-22 23:59",
            "Canvas ID": 55,
        }
    ]
    syllabus = [
        {
            "Course": "MTH 201",
            "Task": "A2",
            "Due Date": "2026-09-22 23:59",
        }
    ]
    kept, stats = dedupe_cross_source(canvas, syllabus)
    assert stats.kept == 1
    assert kept[0]["Course"] == "MATH 201"
    assert kept[0][TASK_ID_COL] == canvas_task_id(55)
