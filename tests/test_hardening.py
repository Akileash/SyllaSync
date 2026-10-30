"""Unit tests for Sylla Sync hardening (dedupe, dates, schedule, calendar)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from syllasync.canvas_module import section_allowed
from syllasync.date_utils import (
    calendar_event_bounds,
    due_window_bounds,
    has_explicit_time,
    is_session_event,
    normalize_calendar_date,
    parse_due_datetime,
)
from syllasync.dedupe_module import (
    TASK_ID_COL,
    canvas_task_id,
    dedupe_cross_source,
    fuzzy_title_key,
    is_phantom_task,
    promote_task_id,
    resolve_persisted_task_id,
    syllabus_task_id,
)
from syllasync.google_calendar_module import compute_obsolete_event_ids
from syllasync.vocab import calendar_color, calendar_summary_prefix, normalize_status, sheet_status


# ---------------------------------------------------------------------------
# Deduplication & ID promotion
# ---------------------------------------------------------------------------


def test_fuzzy_title_variants_match():
    assert fuzzy_title_key("Midterm Exam") == fuzzy_title_key("Midterm 1")
    assert fuzzy_title_key("Assignment 2 - Written") == fuzzy_title_key("A2")
    assert fuzzy_title_key("Homework 3") == fuzzy_title_key("HW 3")


def test_syllabus_to_canvas_promotion(tmp_path, monkeypatch):
    state_file = tmp_path / ".syllasync_task_state.json"
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", state_file)

    canvas = [
        {
            "Course": "COURSE 101",
            "Task": "Assignment 1",
            "Due Date": "2026-09-15 23:59",
            "Canvas ID": 4242,
        }
    ]
    syllabus = [
        {
            "Course": "COURSE 101",
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
            "course 101||assignment#1": {"task_id": "syllabus_course_101_a1_2026-09-15"},
        },
        "aliases": {},
    }
    preferred = canvas_task_id(99)
    result = resolve_persisted_task_id("COURSE 101", "Assignment 1", preferred, state)
    assert result == preferred
    assert state["aliases"]["syllabus_course_101_a1_2026-09-15"] == preferred


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
        "syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json"
    )
    kept, stats = dedupe_cross_source(
        [],
        [
            {"Course": "COURSE 101", "Task": "Labs", "Due Date": "2026-09-08"},
            {"Course": "COURSE 302", "Task": "Quizzes", "Due Date": "TBD"},
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
    import syllasync.date_utils as date_utils

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


def test_homework_deadline_still_ends_at_due_time(monkeypatch):
    import syllasync.date_utils as date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    bounds = calendar_event_bounds("2026-10-09 16:00", title="HW1")
    assert bounds is not None
    assert bounds["all_day"] is False
    # Deadline convention: event ends at due, starts 60 minutes earlier.
    assert "2026-10-09T15:00:00" in bounds["start"]["dateTime"]
    assert "2026-10-09T16:00:00" in bounds["end"]["dateTime"]
    assert "-06:00" in bounds["start"]["dateTime"]


def _edmonton_offset_suffix(year: int, month: int, day: int, hour: int = 17, minute: int = 30) -> str:
    """Return the RFC3339 offset suffix ZoneInfo reports for America/Edmonton."""
    dt = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/Edmonton"))
    # e.g. '-06:00' (MDT) or '-07:00' (MST)
    return dt.isoformat()[-6:]


def test_exam_session_starts_at_parsed_time_mdt_october(monkeypatch):
    """COURSE 101 Midterm 1: 17:30-18:30 America/Edmonton during MDT."""
    import syllasync.date_utils as date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    assert is_session_event("Midterm 1") is True
    bounds = calendar_event_bounds(
        "2026-10-09 17:30",
        title="Midterm 1",
        source="Syllabus",
        end_value="2026-10-09 18:30",
    )
    assert bounds is not None
    assert bounds["all_day"] is False
    assert bounds["start"]["dateTime"].startswith("2026-10-09T17:30:00")
    assert bounds["end"]["dateTime"].startswith("2026-10-09T18:30:00")
    offset = _edmonton_offset_suffix(2026, 10, 9)
    assert bounds["start"]["dateTime"].endswith(offset)
    assert bounds["end"]["dateTime"].endswith(offset)
    # October should be daylight time on a correct tz database.
    assert offset in {"-06:00", "-07:00"}
    assert bounds["start"]["timeZone"] == "America/Edmonton"


def test_exam_session_starts_at_parsed_time_mst_november(monkeypatch):
    """COURSE 101 Midterm 2: 17:30-18:30 America/Edmonton after DST ends (MST)."""
    import syllasync.date_utils as date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    bounds = calendar_event_bounds(
        "2026-11-20 17:30",
        title="Midterm 2",
        source="Syllabus",
        end_value="2026-11-20 18:30",
    )
    assert bounds is not None
    assert bounds["start"]["dateTime"].startswith("2026-11-20T17:30:00")
    assert bounds["end"]["dateTime"].startswith("2026-11-20T18:30:00")
    offset = _edmonton_offset_suffix(2026, 11, 20)
    assert bounds["start"]["dateTime"].endswith(offset)
    assert bounds["end"]["dateTime"].endswith(offset)
    # If tzdata is correct, November is standard time (-07:00) while October is -06:00.
    oct_offset = _edmonton_offset_suffix(2026, 10, 9)
    if oct_offset != offset:
        assert offset == "-07:00"
        assert oct_offset == "-06:00"


def test_ece_midterm_uses_parsed_end_duration(monkeypatch):
    import syllasync.date_utils as date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    bounds = calendar_event_bounds(
        "2026-10-15 11:00",
        title="Midterm",
        source="Syllabus",
        end_value="2026-10-15 12:20",
    )
    assert bounds is not None
    assert bounds["start"]["dateTime"].startswith("2026-10-15T11:00:00")
    assert bounds["end"]["dateTime"].startswith("2026-10-15T12:20:00")
    assert "-06:00" in bounds["start"]["dateTime"]


def test_due_window_includes_today_through_end_of_day(monkeypatch):
    import syllasync.date_utils as date_utils

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
            {"course": "COURSE 202", "type": "LAB", "section": "EL05"},
            {"course": "COURSE 202", "type": "LEC", "section": "A1"},
        ]
    }
    assert section_allowed("COURSE 202 LAB EL05", schedule) is True
    assert section_allowed("COURSE 202 LEC A1", schedule) is True


def test_section_filter_rejects_other_lab():
    schedule = {
        "sections": [
            {"course": "COURSE 202", "type": "LAB", "section": "EL05"},
        ]
    }
    assert section_allowed("COURSE 202 LAB EL99", schedule) is False


def test_section_filter_allows_parent_shell_without_section_letters():
    schedule = {
        "sections": [
            {"course": "COURSE 302", "type": "LEC", "section": "A1"},
        ]
    }
    # Parent course shells often omit LEC/LAB markers
    assert section_allowed("COURSE 302 - Fall 2026", schedule) is True


def test_section_filter_noop_when_schedule_empty():
    assert section_allowed("ANY COURSE LAB Z9", {"sections": []}) is True


# ---------------------------------------------------------------------------
# Masterlist sort order
# ---------------------------------------------------------------------------


def test_sort_submitted_sinks_to_bottom():
    """Submitted/Complete rows always sort below active work, then by due date."""
    import pandas as pd
    from syllasync.sheets_module import sort_by_days_until_due

    df = pd.DataFrame(
        [
            {
                "Course": "COURSE 301",
                "Assessment title": "Old submitted",
                "Due Date": "2026-09-16 16:00",
                "Status": "Submitted",
                "Priority": "",
                "Estimated time dedicated to task": "",
                "Task_ID": "a",
                "Is Draft": False,
            },
            {
                "Course": "COURSE 101",
                "Assessment title": "HW2",
                "Due Date": "2026-09-24 17:00",
                "Status": "Not Started",
                "Priority": "",
                "Estimated time dedicated to task": "",
                "Task_ID": "b",
                "Is Draft": False,
            },
            {
                "Course": "COURSE 401",
                "Assessment title": "A1",
                "Due Date": "2026-09-21 16:00",
                "Status": "In Progress",
                "Priority": "",
                "Estimated time dedicated to task": "",
                "Task_ID": "c",
                "Is Draft": False,
            },
            {
                "Course": "COURSE 202",
                "Assessment title": "TBD item",
                "Due Date": "",
                "Status": "Not Started",
                "Priority": "",
                "Estimated time dedicated to task": "",
                "Task_ID": "d",
                "Is Draft": False,
            },
            {
                "Course": "COURSE 302",
                "Assessment title": "Done quiz",
                "Due Date": "2026-09-16 08:00",
                "Status": "Complete",
                "Priority": "",
                "Estimated time dedicated to task": "",
                "Task_ID": "e",
                "Is Draft": False,
            },
        ]
    )
    sorted_df = sort_by_days_until_due(df)
    assert sorted_df["Assessment title"].tolist() == [
        "A1",
        "HW2",
        "TBD item",
        "Done quiz",
        "Old submitted",
    ]


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


def test_calendar_hw1_does_not_match_hw10():
    """Regression: short titles must not collide via startswith (HW1 vs HW10)."""
    from syllasync.google_calendar_module import _collect_match_candidates

    existing = [
        {
            "id": "hw10",
            "summary": "COURSE 101 - HW10",
            "task_id": "syllabus_course_101_hw10_2026-12-03",
            "fuzzy_key": "course 101||homework#10",
            "start_key": "2026-12-03T16:00:00-07:00",
        },
        {
            "id": "hw2",
            "summary": "COURSE 101 - HW2",
            "task_id": "syllabus_course_101_hw2_2026-09-24",
            "fuzzy_key": "course 101||homework#2",
            "start_key": "2026-09-24T16:00:00-06:00",
        },
    ]
    candidates = _collect_match_candidates(
        task_id="canvas_99",
        fuzzy="course 101||homework#1",
        summary="[SUBMITTED] COURSE 101 - HW1",
        by_task_id={},
        by_fuzzy={},
        existing_list=existing,
    )
    assert candidates == []


def test_calendar_long_truncated_titles_still_match():
    from syllasync.google_calendar_module import _collect_match_candidates

    existing = [
        {
            "id": "long1",
            "summary": "COURSE 501 - Assignment One - 2026 Co-op Expectations",
            "task_id": "canvas_1",
            "fuzzy_key": "course 501||assignment#1",
            "start_key": "2026-09-16",
        }
    ]
    candidates = _collect_match_candidates(
        task_id="canvas_1",
        fuzzy="course 501||assignment#1",
        summary="COURSE 501 - Assignment One - 2026 Co-op Expectations Agreement",
        by_task_id={},
        by_fuzzy={},
        existing_list=existing,
    )
    assert len(candidates) == 1
    assert candidates[0]["id"] == "long1"


def test_build_event_sets_confirmed_status():
    from syllasync.google_calendar_module import _build_event

    event = _build_event(
        {
            "Course": "COURSE 101",
            "Assessment title": "HW1",
            "Due Date": "2026-09-17 17:00",
            "Status": "Submitted",
            "Priority": "",
            "Task_ID": "canvas_99",
            "Is Draft": False,
        }
    )
    assert event is not None
    assert event["status"] == "confirmed"
    assert event["summary"] == "[SUBMITTED] COURSE 101 - HW1"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_vocab_status_sheet_mapping():
    assert sheet_status("not started") == "Not Started"
    assert sheet_status("submitted") == "Submitted"
    assert normalize_status("done").value == "SUBMITTED"


def test_calendar_status_prefix_and_color():
    assert calendar_summary_prefix("Complete").startswith("[SUBMITTED]")
    assert calendar_summary_prefix("Submitted").startswith("[SUBMITTED]")
    assert calendar_color("Complete", "High") == "2"
    assert calendar_color("Not Started", "High") == "11"


def test_discord_skips_completed_status():
    from syllasync.discord_module import _is_finished_status

    assert _is_finished_status("Complete") is True
    assert _is_finished_status("Completed") is True
    assert _is_finished_status("Done") is True
    assert _is_finished_status("Submitted") is True
    assert _is_finished_status("Graded") is True
    assert _is_finished_status("Not Started") is False
    assert _is_finished_status("In Progress") is False


def test_canvas_utc_due_converts_to_local(monkeypatch):
    import syllasync.date_utils as date_utils

    monkeypatch.setattr(date_utils, "LOCAL_TIMEZONE", "America/Edmonton")
    # 2026-09-17 05:59 UTC == 2026-09-16 23:59 MDT
    assert (
        date_utils.format_internal_datetime("2026-09-17T05:59:00Z")
        == "2026-09-16 23:59"
    )


def test_assignment_one_fuzzy_key():
    from syllasync.dedupe_module import fuzzy_match_key, fuzzy_title_key

    assert fuzzy_title_key("Assignment One - 2026 Co-op Agreement") == "assignment#1"
    assert fuzzy_title_key("Assignment 1") == "assignment#1"
    assert fuzzy_match_key("COURSE 501", "Assignment One - 2026 Co-op") == fuzzy_match_key(
        "COURSE 501", "Assignment 1"
    )


def test_split_title_and_due_moves_date_out_of_title(monkeypatch):
    import syllasync.date_utils as date_utils

    monkeypatch.setattr("syllasync.config.TERM_YEAR", 2026)

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


def test_mth_vs_math_101w_normalize_same():
    from syllasync.course_utils import courses_equivalent, normalize_course_code, prefer_course_label
    from syllasync.dedupe_module import fuzzy_match_key

    # Abbreviation collapse (MTH -> MATH) with a made-up catalog number.
    assert normalize_course_code("MTH 101") == "MATH 101"
    assert normalize_course_code("Math 101W") == "MATH 101"
    assert normalize_course_code("MATH 101") == "MATH 101"
    assert courses_equivalent("MTH 101", "MATH 101W")
    assert prefer_course_label("MTH 101", "MATH 101W") == "MATH 101"
    assert fuzzy_match_key("MTH 101", "Assignment 1") == fuzzy_match_key(
        "MATH 101W", "Assignment 1"
    )
    # Generic COURSE / CRS alias used in fixtures.
    assert normalize_course_code("CRS 101") == "COURSE 101"
    assert normalize_course_code("COURSE 101W") == "COURSE 101"


def test_merge_drops_stale_labs_placeholder():
    from syllasync.sheets_module import merge_tracker

    existing = pd.DataFrame(
        [
            {
                "Course": "COURSE 101",
                "Assessment title": "Labs",
                "Due Date": "2026-09-08",
                "Estimated time dedicated to task": "",
                "Status": "Not Started",
                "Priority": "",
                TASK_ID_COL: "syllabus_labs",
                "Is Draft": False,
            },
            {
                "Course": "COURSE 101",
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
                "Course": "COURSE 101",
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
        "syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json"
    )
    canvas = [
        {
            "Course": "COURSE 101W",
            "Task": "Assignment 2",
            "Due Date": "2026-09-22 23:59",
            "Canvas ID": 55,
        }
    ]
    syllabus = [
        {
            "Course": "CRS 101",
            "Task": "A2",
            "Due Date": "2026-09-22 23:59",
        }
    ]
    kept, stats = dedupe_cross_source(canvas, syllabus)
    assert stats.kept == 1
    assert kept[0]["Course"] == "COURSE 101"
    assert kept[0][TASK_ID_COL] == canvas_task_id(55)


def test_syllabus_exam_keeps_start_of_clock_range():
    """A syllabus clock range must not be stored as a date-only due.

    "5:30- 6:30pm" keeps the start (17:30), not the end (18:30).
    """
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import parse_syllabus_exam_text

    text = "First midterm exam 25% Friday, October 9, 5:30- 6:30pm"
    rows = parse_syllabus_exam_text(text, "COURSE 101")
    dated = [r for r in rows if str(r["Due Date"]).upper() not in {"", "TBD"}]
    assert len(dated) == 1
    assert dated[0]["Due Date"] == f"{TERM_YEAR}-10-09 17:30"
    assert "18:30" not in dated[0]["Due Date"]


def test_timed_duplicate_wins_over_date_only(tmp_path, monkeypatch):
    """Collapsing the same task must keep a clock time over a date-only due."""
    monkeypatch.setattr(
        "syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json"
    )
    date_only = {
        "Course": "COURSE 101",
        "Task": "Midterm 1",
        "Due Date": "2026-10-09",
    }
    timed = {
        "Course": "COURSE 101",
        "Task": "Midterm 1",
        "Due Date": "2026-10-09 17:30",
    }
    for first, second in ((date_only, timed), (timed, date_only)):
        kept, _stats = dedupe_cross_source([], [dict(first), dict(second)])
        assert len(kept) == 1
        assert kept[0]["Due Date"] == "2026-10-09 17:30"

    from syllasync.sheets_module import _collapse_by_match_key

    forward = pd.DataFrame(
        [
            {
                "Course": "COURSE 101",
                "Assessment title": "Midterm 1",
                "Due Date": "2026-10-09",
            },
            {
                "Course": "COURSE 101",
                "Assessment title": "Midterm 1",
                "Due Date": "2026-10-09 17:30",
            },
        ]
    )
    backward = forward.iloc[::-1].reset_index(drop=True)
    for frame in (forward, backward):
        collapsed = _collapse_by_match_key(frame)
        assert len(collapsed) == 1
        assert collapsed.iloc[0]["Due Date"] == "2026-10-09 17:30"


def test_gemini_uses_filename_catalog_course_over_inferred_title():
    """Gemini Course titles must yield to a catalog code from the PDF filename.

    Regression: lab schedule PDFs named like
    "COURSE 202 - Lab Problem Schedule.pdf" fall through the weekly parser
    (empty matches) to Gemini; without a filename override, Gemini may
    label rows "Advanced Seminar", which is not courses_equivalent
    to COURSE 202 and would pollute the tracker.
    """
    import json

    from syllasync.course_utils import courses_equivalent, normalize_course_code
    from syllasync.syllabus_module import (
        _course_from_filename,
        _parse_gemini_response,
        parse_weekly_schedule_text,
    )

    filename = "COURSE 202 - Lab Problem Schedule.pdf"
    from_file = _course_from_filename(filename)
    assert normalize_course_code(from_file) == "COURSE 202"
    assert courses_equivalent("COURSE 202", "Advanced Seminar") is True  # alias
    # Weekly parser has nothing to match on a lab-quiz schedule → Gemini path.
    assert parse_weekly_schedule_text("Lab Quiz 1 Sept 15", from_file) == []

    raw = json.dumps(
        [
            {
                "Course": "Advanced Seminar",
                "Task": "Lab Quiz 1",
                "Due Date": "2026-09-15",
            },
            {
                "Course": "Advanced Seminar",
                "Task": "Lab Quiz 2",
                "Due Date": "2026-09-22",
            },
        ]
    )
    rows = _parse_gemini_response(raw, course_from_filename=from_file)
    assert len(rows) == 2
    assert all(r["Course"] == "COURSE 202" for r in rows)
    assert "Advanced Seminar" not in {r["Course"] for r in rows}
    # Do not invent times — date-only dues stay date-only.
    assert rows[0]["Due Date"] == "2026-09-15"
    assert rows[1]["Due Date"] == "2026-09-22"
    assert rows[0]["Task"] == "Lab Quiz 1"


def test_gemini_keeps_course_when_filename_has_no_catalog_code():
    """Without a catalog code in the filename, Gemini's Course is preserved."""
    import json

    from syllasync.course_utils import normalize_course_code
    from syllasync.syllabus_module import _course_from_filename, _parse_gemini_response

    filename = "Weekly Readings Overview.pdf"
    from_file = _course_from_filename(filename)
    assert normalize_course_code(from_file) is None

    raw = json.dumps(
        [{"Course": "Intro Seminar", "Task": "Essay", "Due Date": "2026-10-01"}]
    )
    rows = _parse_gemini_response(raw, course_from_filename=from_file)
    assert len(rows) == 1
    assert rows[0]["Course"] == "Intro Seminar"


# ---------------------------------------------------------------------------
# COURSE 301 / COURSE 401 midterm capture, announcements, stale-year Canvas dues
# ---------------------------------------------------------------------------


def test_course301_midterm_range_without_start_meridiem():
    """'Oct. 15 at 11-12:20 pm' is 11:00 (not 23:00); the 'Oct.' period parses."""
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import _due_with_following_clock, parse_syllabus_exam_text

    text = (
        "Laboratories Synchronous Specified in each lab 15% Midterm exam Synchronous "
        "Tentatively Thursday, Oct. 15 at 11-12:20 pm25% Final exam Synchronous UofA "
        "will post the exam schedule online"
    )
    rows = parse_syllabus_exam_text(text, "COURSE 301")
    midterms = [r for r in rows if r["Task"] == "Midterm"]
    assert len(midterms) == 1
    assert midterms[0]["Due Date"] == f"{TERM_YEAR}-10-15 11:00"
    assert _due_with_following_clock("2026-10-15", " at 11-12:20 pm") == "2026-10-15 11:00"
    # Existing behaviour kept: end-only meridiem on an afternoon range.
    assert _due_with_following_clock("2026-10-09", ", 5:30- 6:30pm") == "2026-10-09 17:30"
    assert _due_with_following_clock("2026-10-24", " 12:30 - 2:00 PM") == "2026-10-24 12:30"
    # No meridiem anywhere -> no invented time.
    assert _due_with_following_clock("2026-10-15", " at 11-12") == "2026-10-15"


def test_course_from_filename_does_not_glue_syllabus_letter():
    from syllasync.syllabus_module import _course_from_filename

    assert _course_from_filename("COURSE301SYLLABUS.pdf") == "COURSE 301"
    assert _course_from_filename("COURSE301_Syllabus.pdf") == "COURSE 301"
    assert _course_from_filename("COURSE101_Syllabus.pdf") == "COURSE 101"
    assert _course_from_filename("COURSE401_Syllabus.pdf") == "COURSE 401"
    assert _course_from_filename("COURSE101_Weekly_Schedule.pdf") == "COURSE 101"


def test_exam_period_sentence_does_not_create_dated_final():
    from syllasync.syllabus_module import parse_syllabus_exam_text

    text = (
        "Final exam Synchronous UofA will post the exam schedule online to the Exam "
        "Schedules web page in October45% The final exam period for the Fall 2026 "
        "term is from December 9 to December 22, 2026. The specific final exam "
        "schedule will be released in October."
    )
    rows = parse_syllabus_exam_text(text, "COURSE 301")
    finals = [r for r in rows if r["Task"] == "Final Exam"]
    assert len(finals) == 1
    assert finals[0]["Due Date"] in {"", "TBD"}
    assert not any("12-09" in str(r["Due Date"]) for r in rows)


class _FakeTopic:
    def __init__(self, topic_id, title, message, posted_at):
        self.id = topic_id
        self.title = title
        self.message = message
        self.posted_at = posted_at


class _FakeAssignment:
    def __init__(self, assignment_id, name, due_at, published=True):
        self.id = assignment_id
        self.name = name
        self.due_at = due_at
        self.published = published


class _FakeCourse:
    def __init__(self, course_id, code, topics=(), assignments=(), topics_error=None):
        self.id = course_id
        self.course_code = code
        self.name = code
        self._topics = list(topics)
        self._assignments = list(assignments)
        self._topics_error = topics_error

    def get_discussion_topics(self, **kwargs):
        assert kwargs.get("only_announcements") is True
        if self._topics_error:
            raise self._topics_error
        return list(self._topics)

    def get_assignments(self, **kwargs):
        return list(self._assignments)


class _FakeCanvas:
    def __init__(self, courses=(), error=None):
        self._courses = list(courses)
        self._error = error

    def get_courses(self, **kwargs):
        if self._error:
            raise self._error
        return list(self._courses)


def test_announcement_midterm_upgrades_undated_syllabus_row(tmp_path, monkeypatch):
    import syllasync.canvas_module as canvas_module
    from syllasync.sheets_module import _collapse_by_match_key
    from syllasync.syllabus_module import parse_announcement_exam_text

    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    monkeypatch.setattr(canvas_module, "CANVAS_READ_ANNOUNCEMENTS", True)
    monkeypatch.setattr(canvas_module, "ALLOWED_COURSES", "COURSE 401,COURSE 302")
    monkeypatch.setattr(canvas_module, "TERM_YEAR", 2026)
    monkeypatch.setattr(canvas_module, "load_schedule", lambda: {"sections": []})

    text = "The midterm will be in CLASS on OCT 23 (Friday)."
    parsed = parse_announcement_exam_text(text, "COURSE 401", base_year=2026, posted_month=9)
    assert [(r["Course"], r["Task"], r["Due Date"]) for r in parsed] == [
        ("COURSE 401", "Midterm", "2026-10-23")
    ]
    # Review / solutions sentences are not exam sittings.
    assert parse_announcement_exam_text("Midterm review session on Oct 20.", "COURSE 401") == []

    canvas = _FakeCanvas(
        [
            _FakeCourse(
                34510,
                "COURSE 401 - Fall 2026",
                topics=[
                    _FakeTopic(
                        1,
                        "Midterm Exam Date",
                        "<p>Hello class,</p><p>The midterm will be in CLASS on OCT 23 (Friday).</p>",
                        "2026-09-11T17:00:00Z",
                    )
                ],
            ),
            _FakeCourse(99, "COURSE 302", topics_error=RuntimeError("403 forbidden")),
        ]
    )
    rows = canvas_module.fetch_canvas_announcement_exams(canvas=canvas)
    assert [(r["Course"], r["Task"], r["Due Date"]) for r in rows] == [
        ("COURSE 401", "Midterm", "2026-10-23")
    ]

    undated = {"Source": "Syllabus", "Course": "COURSE 401", "Task": "Midterm", "Due Date": ""}
    final_tbd = {"Source": "Syllabus", "Course": "COURSE 401", "Task": "Final Exam", "Due Date": ""}
    for syllabus in ([dict(undated), dict(final_tbd), *rows], [*rows, dict(undated), dict(final_tbd)]):
        kept, _stats = dedupe_cross_source([], [dict(r) for r in syllabus])
        midterms = [r for r in kept if r["Course"] == "COURSE 401" and r["Task"] == "Midterm"]
        assert len(midterms) == 1
        assert midterms[0]["Due Date"] == "2026-10-23"
        assert midterms[0]["Is Draft"] is False
        finals = [r for r in kept if r["Task"] == "Final Exam"]
        assert finals and finals[0]["Due Date"] in {"", "TBD"}

    # A timed due still beats date-only; date-only beats empty/TBD.
    kept, _ = dedupe_cross_source(
        [],
        [
            dict(undated),
            {"Course": "COURSE 401", "Task": "Midterm", "Due Date": "2026-10-23"},
            {"Course": "COURSE 401", "Task": "Midterm", "Due Date": "2026-10-23 11:00"},
        ],
    )
    assert kept[0]["Due Date"] == "2026-10-23 11:00"

    # An undated Final Exam must not take a Deferred Final's date.
    kept, _ = dedupe_cross_source(
        [],
        [
            {"Course": "COURSE 101", "Task": "Final Exam", "Due Date": ""},
            {"Course": "COURSE 101", "Task": "Deferred Final Exam", "Due Date": "2027-01-09"},
        ],
    )
    assert kept[0]["Task"] == "Final Exam"
    assert kept[0]["Due Date"] in {"", "TBD"}

    frame = pd.DataFrame(
        [
            {"Course": "COURSE 401", "Assessment title": "Midterm", "Due Date": "TBD"},
            {"Course": "COURSE 401", "Assessment title": "Midterm", "Due Date": "2026-10-23"},
        ]
    )
    for f in (frame, frame.iloc[::-1].reset_index(drop=True)):
        collapsed = _collapse_by_match_key(f)
        assert len(collapsed) == 1
        assert collapsed.iloc[0]["Due Date"] == "2026-10-23"


def test_announcement_scan_never_raises(monkeypatch):
    import syllasync.canvas_module as canvas_module

    monkeypatch.setattr(canvas_module, "CANVAS_READ_ANNOUNCEMENTS", True)
    monkeypatch.setattr(canvas_module, "load_schedule", lambda: {"sections": []})
    broken = _FakeCanvas(error=RuntimeError("boom"))
    assert canvas_module.fetch_canvas_announcement_exams(canvas=broken) == []
    monkeypatch.setattr(canvas_module, "CANVAS_READ_ANNOUNCEMENTS", False)
    assert canvas_module.fetch_canvas_announcement_exams(canvas=broken) == []


def test_stale_year_canvas_due_is_warned_not_converted(monkeypatch, caplog):
    import logging

    import syllasync.canvas_module as canvas_module

    course = _FakeCourse(
        36126,
        "COURSE 302 LEC A2 - Fall 2026 - INTRO DIGITAL LOGIC DESIGN",
        assignments=[
            _FakeAssignment(794672, "Midterm", "2025-10-30T18:30:00Z"),
            _FakeAssignment(794653, "Assignment 1", "2026-09-18T05:59:59Z"),
        ],
    )
    monkeypatch.setattr(canvas_module, "Canvas", lambda url, token: _FakeCanvas([course]))
    monkeypatch.setattr(canvas_module, "CANVAS_URL", "https://canvas.example")
    monkeypatch.setattr(canvas_module, "CANVAS_TOKEN", "fake")
    monkeypatch.setattr(canvas_module, "ALLOWED_COURSES", "COURSE 302")
    monkeypatch.setattr(canvas_module, "TERM_YEAR", 2026)

    with caplog.at_level(logging.WARNING, logger="canvas_module"):
        rows = canvas_module.fetch_canvas_assignments(lookback_days=100000, enforce_schedule=False)

    assert "stale-year due date" in caplog.text
    assert [w["Canvas ID"] for w in canvas_module.STALE_YEAR_WARNINGS] == [794672]
    midterm = [r for r in rows if r["Task"] == "Midterm"]
    # Kept as-is (when inside the window) - never rewritten to the term year.
    assert midterm and str(midterm[0]["Due Date"]).startswith("2025-10-30")
    assert not any(str(r["Due Date"]).startswith("2026-10-30") for r in rows)
    assert canvas_module.is_stale_year_due("2026-10-30", 2026) is False
    assert canvas_module.is_stale_year_due("No due date", 2026) is False


# --- Term-year coercion for syllabus/Gemini dates (2020 Lab Quiz bug) ---
from syllasync.date_utils import coerce_to_term_year
from syllasync.syllabus_module import _parse_gemini_response, SYLLABUS_PROMPT


def test_coerce_wrong_fall_year_to_term():
    assert coerce_to_term_year("2020-09-21", 2026) == "2026-09-21"
    assert coerce_to_term_year("2020-11-30 14:00", 2026) == "2026-11-30 14:00"


def test_coerce_winter_month_to_next_year():
    assert coerce_to_term_year("2026-01-15", 2026) == "2027-01-15"
    assert coerce_to_term_year("2027-04-10", 2026) == "2027-04-10"


def test_coerce_leaves_tbd_and_correct_dates():
    assert coerce_to_term_year("TBD", 2026) == "TBD"
    assert coerce_to_term_year("2026-10-05", 2026) == "2026-10-05"


def test_gemini_response_with_2020_dates_lands_in_term_year(monkeypatch):
    import syllasync.syllabus_module as syllabus_module
    monkeypatch.setattr("syllasync.config.TERM_YEAR", 2026)
    raw = '[{"Course": "COURSE 202", "Task": "Lab Quiz 1", "Due Date": "2020-09-21"},' \
          ' {"Course": "COURSE 202", "Task": "Lab Quiz 5", "Due Date": "2020-11-30"}]'
    rows = _parse_gemini_response(raw, "COURSE 202 - Lab Problem Schedule")
    dues = [r["Due Date"][:10] for r in rows]
    assert dues == ["2026-09-21", "2026-11-30"]


def test_prompt_states_term_year():
    prompt = SYLLABUS_PROMPT.format(text="x", term_year=2026, next_year=2027)
    assert "Fall 2026" in prompt and "2027" in prompt


# --- Advanced Seminar alias / COURSE 202 lab quiz dedupe ---
from syllasync.course_utils import normalize_course_code as _ncc
from syllasync.date_utils import apply_lab_monday_due as _m209


def test_advanced_seminar_maps_to_course202():
    assert _ncc("Advanced Seminar") == "COURSE 202"
    assert _ncc("  advanced   seminar ") == "COURSE 202"


def test_seminar_lab_quiz_snaps_to_monday():
    assert _m209("Advanced Seminar", "Lab Quiz 1", "2026-09-25").startswith("2026-09-21")


def test_dedupe_merges_seminar_and_course202_lab_quiz(tmp_path, monkeypatch):
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / "state.json")
    syllabus = [
        {"Source": "Syllabus", "Course": "COURSE 202", "Task": "Lab Quiz 3", "Due Date": "2026-10-26"},
        {"Source": "Syllabus", "Course": "Advanced Seminar", "Task": "Lab Quiz 3", "Due Date": "2026-10-30"},
    ]
    kept, _ = dedupe_cross_source([], syllabus)
    quiz = [r for r in kept if r["Task"] == "Lab Quiz 3"]
    assert len(quiz) == 1
    assert quiz[0]["Course"] == "COURSE 202"
    assert str(quiz[0]["Due Date"]).startswith("2026-10-26")


def test_sheet_collapse_keeps_status_and_monday():
    import pandas as pd
    from syllasync.sheets_module import _collapse_by_match_key
    df = pd.DataFrame([
        {"Course": "Advanced Seminar", "Assessment title": "Lab Quiz 1", "Due Date": "2026-09-25", "Status": "Submitted"},
        {"Course": "COURSE 202", "Assessment title": "Lab Quiz 1", "Due Date": "2026-09-21", "Status": ""},
    ])
    out = _collapse_by_match_key(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["Course"] == "COURSE 202"
    assert str(row["Due Date"]).startswith("2026-09-21")
    assert row["Status"] == "Submitted"


# --- Grading-weight percentages must not become exam titles ---
from syllasync.syllabus_module import parse_syllabus_exam_text as _pset


def test_grade_weight_not_in_midterm_title():
    rows = _pset("Midterm 30 % Oct 24, 12:30 - 2:00 PM\nFinal Exam 50 % TBA", "COURSE 202")
    tasks = [r["Task"] for r in rows]
    assert "Midterm 30" not in tasks
    mid = [r for r in rows if r["Task"] == "Midterm"]
    assert len(mid) == 1 and mid[0]["Due Date"].endswith("12:30")
    assert not any(t.startswith("Final 50") for t in tasks)


def test_grade_weight_no_space_and_final():
    rows = _pset("Midterm 30% Oct24,12:30-2:00PM\nFinal 40% Dec 15", "COURSE 202")
    tasks = [r["Task"] for r in rows]
    assert "Midterm 30" not in tasks and "Final 40" not in tasks


def test_numbered_midterm_still_kept():
    rows = _pset("Midterm 2 Nov 20, 5:30 PM", "COURSE 101")
    assert any(r["Task"] == "Midterm 2" for r in rows)


def test_midterm_and_midterm_exam_share_fuzzy_key():
    from syllasync.dedupe_module import fuzzy_match_key
    assert fuzzy_match_key("COURSE 202", "Midterm") == fuzzy_match_key("COURSE 202", "Midterm Exam")


# --- Status must never be downgraded by merges; Calendar prefix follows it ---
def test_prefer_status_never_downgrades():
    from syllasync.vocab import prefer_status
    assert prefer_status("Complete", "Submitted") == "Complete"   # typed Complete kept
    assert prefer_status("Submitted", "Complete") == "Submitted"  # stale Complete never wins
    assert prefer_status("Not Started", "Submitted") == "Submitted"
    assert prefer_status("Submitted", "Not Started") == "Submitted"
    assert prefer_status("", "Submitted") == "Submitted"
    assert prefer_status("Not Started", "") == "Not Started"


def test_collapse_keeps_submitted_over_complete_duplicate():
    import pandas as pd
    from syllasync.sheets_module import _collapse_by_match_key
    df = pd.DataFrame([
        {"Course": "COURSE 101W", "Assessment title": "Midterm 1", "Due Date": "2026-10-09 17:30", "Status": "Submitted"},
        {"Course": "COURSE 101", "Assessment title": "Midterm 1", "Due Date": "2026-10-09 17:30", "Status": "Complete"},
    ])
    out = _collapse_by_match_key(df)
    assert len(out) == 1 and out.iloc[0]["Status"] == "Submitted"


def test_calendar_event_summary_reflects_status():
    from syllasync.google_calendar_module import _build_event
    base = {"Course": "COURSE 101", "Assessment title": "Midterm 1", "Due Date": "2026-10-09 17:30",
            "Task_ID": "syllabus_course_101_midterm_1_2026-10-09"}
    assert _build_event({**base, "Status": "Complete"})["summary"].startswith("[SUBMITTED] ")
    assert _build_event({**base, "Status": "Submitted"})["summary"].startswith("[SUBMITTED] ")


def test_calendar_equivalence_detects_status_change():
    from syllasync.google_calendar_module import _build_event, _events_equivalent
    base = {"Course": "COURSE 101", "Assessment title": "Midterm 1", "Due Date": "2026-10-09 17:30",
            "Task_ID": "t1"}
    old = _build_event({**base, "Status": "Submitted"})
    new = _build_event({**base, "Status": "Complete"})
    meta = {"summary": old["summary"], "start_key": old["start"].get("dateTime") or old["start"].get("date"),
            "task_id": "t1", "colorId": str(old["colorId"]),
            "status": old["extendedProperties"]["private"]["status"],
            "priority": old["extendedProperties"]["private"]["priority"]}
    assert _events_equivalent(meta, old)
    assert not _events_equivalent(meta, new)


# --- Manual due/end overrides (due_overrides.json) -------------------------


def test_merge_keeps_existing_timed_due_over_date_only_announcement(tmp_path, monkeypatch):
    from syllasync.sheets_module import merge_tracker

    # Never touch the real .syllasync_task_state.json
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")

    existing = pd.DataFrame([{
        "Course": "COURSE 401", "Assessment title": "Midterm",
        "Due Date": "2026-10-23 11:00", "Status": "Not Started",
        TASK_ID_COL: "test_only_mat_e_midterm_id",
    }])
    incoming = pd.DataFrame([{
        "Course": "COURSE 401", "Assessment title": "Midterm",
        "Due Date": "2026-10-23", TASK_ID_COL: "test_only_mat_e_midterm_id",
        "Source": "Syllabus", "Is Draft": False,
    }])
    out = merge_tracker(existing, incoming)
    row = out[out["Assessment title"] == "Midterm"].iloc[0]
    assert str(row["Due Date"]).startswith("2026-10-23 11:00")


def test_due_override_sets_timed_due_and_end_and_calendar_bounds(tmp_path):
    from syllasync.date_utils import calendar_event_bounds
    from syllasync.sheets_module import apply_due_overrides, load_due_overrides

    path = tmp_path / "due_overrides.json"
    path.write_text(json.dumps({"overrides": [{
        "course": "COURSE 401", "title": "Midterm",
        "due": "2026-10-23 11:00", "end": "2026-10-23 11:50",
    }]}), encoding="utf-8")
    overrides = load_due_overrides(path)
    df = pd.DataFrame([
        {"Course": "COURSE 401", "Assessment title": "Midterm", "Due Date": "2026-10-23"},
        {"Course": "COURSE 101", "Assessment title": "Midterm 2", "Due Date": "2026-11-20 17:30"},
    ])
    out = apply_due_overrides(df, overrides)
    mat = out.iloc[0]
    assert mat["Due Date"] == "2026-10-23 11:00"
    assert mat["End Date"] == "2026-10-23 11:50"
    # Untouched row keeps its own due and gets no end
    assert out.iloc[1]["Due Date"] == "2026-11-20 17:30"
    assert out.iloc[1]["End Date"] == ""

    bounds = calendar_event_bounds(mat["Due Date"], title="Midterm", source="Syllabus", end_value=mat["End Date"])
    assert "T11:00:00" in bounds["start"]["dateTime"]
    assert "T11:50:00" in bounds["end"]["dateTime"]


def test_due_overrides_missing_file_is_noop(tmp_path):
    from syllasync.sheets_module import apply_due_overrides, load_due_overrides

    assert load_due_overrides(tmp_path / "nope.json") == {}
    df = pd.DataFrame([{"Course": "X 1", "Assessment title": "A", "Due Date": "2026-01-01"}])
    assert apply_due_overrides(df, {}).equals(df)


# ---------------------------------------------------------------------------
# Canvas-ID identity beats fuzzy title (Lab 1 Pre-lab vs Lab 1 Report)
# ---------------------------------------------------------------------------


def _lab_row(title, tid, due, status=""):
    return {
        "Course": "COURSE 302", "Assessment title": title, "Due Date": due,
        "Estimated time dedicated to task": "", "Status": status, "Priority": "",
        TASK_ID_COL: tid, "Is Draft": False,
    }


def test_distinct_canvas_ids_with_same_fuzzy_title_stay_separate(tmp_path, monkeypatch):
    from syllasync.sheets_module import merge_tracker

    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    assert fuzzy_title_key("Lab 1 Pre-lab Submission") == fuzzy_title_key("Lab 1 Report Submission")
    existing = pd.DataFrame([_lab_row("Lab 1 Pre-lab Submission", "canvas_783548", "2026-10-15 14:00", "Submitted")])
    incoming = pd.DataFrame([
        _lab_row("Lab 1 Pre-lab Submission", "canvas_783548", "2026-10-15 14:00"),
        _lab_row("Lab 1 Report Submission", "canvas_783549", "2026-10-29 14:00"),
    ])
    out = merge_tracker(existing, incoming)
    rows = {r[TASK_ID_COL]: r for r in out.to_dict(orient="records")}
    assert set(rows) == {"canvas_783548", "canvas_783549"}
    assert rows["canvas_783548"]["Assessment title"] == "Lab 1 Pre-lab Submission"
    assert rows["canvas_783548"]["Status"] == "Submitted"
    assert rows["canvas_783549"]["Assessment title"] == "Lab 1 Report Submission"
    assert rows["canvas_783549"]["Status"] in ("", "Not Started")


def test_dedupe_keeps_both_canvas_items_with_same_fuzzy_title(tmp_path, monkeypatch):
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    canvas = [
        {"Course": "COURSE 302", "Task": "Lab 1 Pre-lab Submission", "Due Date": "2026-10-15 14:00", "Canvas ID": 783548},
        {"Course": "COURSE 302", "Task": "Lab 1 Report Submission", "Due Date": "2026-10-29 14:00", "Canvas ID": 783549},
    ]
    kept, _ = dedupe_cross_source(canvas, [])
    assert {r[TASK_ID_COL] for r in kept} == {"canvas_783548", "canvas_783549"}


def test_calendar_candidates_skip_other_canvas_ids():
    from syllasync.google_calendar_module import _collect_match_candidates

    prelab = {"id": "evA", "summary": "COURSE 302 - Lab 1 Pre-lab Submission",
              "task_id": "canvas_783548", "fuzzy_key": "course 302||lab#1"}
    found = _collect_match_candidates(
        task_id="canvas_783549", fuzzy="course 302||lab#1",
        summary="COURSE 302 - Lab 1 Report Submission",
        by_task_id={"canvas_783548": prelab}, by_fuzzy={"course 302||lab#1": [prelab]},
        existing_list=[prelab],
    )
    assert found == []
    same = _collect_match_candidates(
        task_id="canvas_783548", fuzzy="course 302||lab#1",
        summary="COURSE 302 - Lab 1 Pre-lab Submission",
        by_task_id={"canvas_783548": prelab}, by_fuzzy={"course 302||lab#1": [prelab]},
        existing_list=[prelab],
    )
    assert [e["id"] for e in same] == ["evA"]


def test_syllabus_row_still_merges_into_single_canvas_row(tmp_path, monkeypatch):
    from syllasync.sheets_module import merge_tracker

    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    existing = pd.DataFrame([_lab_row("Midterm", "syllabus_course_302_midterm_x", "2026-10-27", "Complete")])
    incoming = pd.DataFrame([_lab_row("Midterm", "canvas_794672", "2026-10-27")])
    out = merge_tracker(existing, incoming)
    assert len(out) == 1
    row = out.to_dict(orient="records")[0]
    assert row[TASK_ID_COL] == "canvas_794672"
    assert row["Status"] == "Complete"


def test_same_title_in_two_canvas_shells_collapses_to_dated_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    canvas = [
        {"Course": "COURSE 301", "Task": "Submit (Lab 1 - Results)", "Due Date": "No due date", "Canvas ID": 802732},
        {"Course": "COURSE 301", "Task": "Submit (Lab 1 - Results)", "Due Date": "2026-10-23 16:30", "Canvas ID": 761091},
        {"Course": "COURSE 301", "Task": "Quiz (Lab 1 - Post Lab)", "Due Date": "2026-10-23 16:30", "Canvas ID": 761054},
    ]
    kept, _ = dedupe_cross_source(canvas, [])
    assert {r[TASK_ID_COL] for r in kept} == {"canvas_761091", "canvas_761054"}


# ---------------------------------------------------------------------------
# Standing rule: the pipeline never marks anything Complete
# ---------------------------------------------------------------------------


def test_sheet_status_never_writes_complete_for_aliases():
    assert sheet_status("Graded") == "Submitted"
    assert sheet_status("Done") == "Submitted"
    assert sheet_status("Submitted") == "Submitted"
    assert sheet_status("Complete") == "Submitted"  # never write Complete


def test_merge_keeps_sheet_submitted_over_stale_complete_state(tmp_path, monkeypatch):
    import json as _json
    from syllasync.sheets_module import merge_tracker

    state_file = tmp_path / ".syllasync_task_state.json"
    state_file.write_text(_json.dumps({"by_match_key": {"course 101||homework#6": {
        "Status": "Complete", "task_id": "syllabus_course_101_hw6_2026-10-22"}}, "aliases": {}}))
    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", state_file)
    row = {"Course": "COURSE 101", "Assessment title": "HW6", "Due Date": "2026-10-22 17:00",
           "Estimated time dedicated to task": "", "Priority": "", "Is Draft": False,
           TASK_ID_COL: "syllabus_course_101_hw6_2026-10-22"}
    existing = pd.DataFrame([{**row, "Status": "Submitted"}])
    incoming = pd.DataFrame([{**row, "Status": ""}])
    out = merge_tracker(existing, incoming)
    assert out.iloc[0]["Status"] == "Submitted"


def test_submitted_is_finished_on_calendar_and_sort():
    from syllasync.google_calendar_module import _build_event
    from syllasync.sheets_module import sort_by_days_until_due

    ev = _build_event({"Course": "COURSE 101", "Assessment title": "HW6", "Due Date": "2026-10-22 17:00",
                       "Status": "Submitted", "Task_ID": "t-hw6"})
    assert ev["summary"] == "[SUBMITTED] COURSE 101 - HW6"
    assert ev["colorId"] == calendar_color("Complete", "High")
    df = pd.DataFrame([
        {"Course": "COURSE 101", "Assessment title": "HW6", "Due Date": "2026-10-22 17:00", "Status": "Submitted"},
        {"Course": "COURSE 101", "Assessment title": "HW7", "Due Date": "2026-10-29 17:00", "Status": "Not Started"},
    ])
    assert list(sort_by_days_until_due(df)["Assessment title"]) == ["HW7", "HW6"]


def test_ignored_items_are_not_re_added(tmp_path, monkeypatch):
    import json as _json
    import syllasync.sheets_module as sheets_module

    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / ".syllasync_task_state.json")
    monkeypatch.setattr("syllasync.config.ALLOWED_COURSES", "")
    ign = tmp_path / "ignored_items.json"
    ign.write_text(_json.dumps({"ignored": [{"task_id": "canvas_814553"}]}))
    monkeypatch.setattr(sheets_module, "IGNORED_ITEMS_PATH", ign)
    canvas = [
        {"Course": "COURSE 101", "Task": "Assignment 1", "Due Date": "No due date", "Canvas ID": 814553},
        {"Course": "COURSE 101", "Task": "Assignment 3", "Due Date": "No due date", "Canvas ID": 818690},
    ]
    frame, _ = sheets_module.incoming_from_sources(canvas, [])
    assert set(frame[TASK_ID_COL]) == {"canvas_818690"}
