"""Syllabus PDF text quality, Due: variants, and Gemini fallback behaviour."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from syllasync.syllabus_module import (
    is_usable_pdf_text,
    parse_weekly_schedule_text,
    parse_due_labeled_items,
    parse_lab_quiz_schedule_text,
    parse_syllabus_exam_text,
    _parse_single_pdf,
    _parse_with_deterministic_parser,
    _normalize_pdf_text,
    SYLLABUS_PARSE_ISSUES,
)


def test_usable_pdf_text_rejects_empty_and_near_empty():
    assert is_usable_pdf_text("") is False
    assert is_usable_pdf_text("   \n\t  ") is False
    assert is_usable_pdf_text("12 13 14") is False
    assert is_usable_pdf_text(".... ... ---") is False
    # A short but real sentence should count as usable.
    assert is_usable_pdf_text(
        "Homework 3 is due Thursday October 12 at 5pm in the collection box."
    )


def test_weekly_parser_accepts_due_label_variants():
    from syllasync.config import TERM_YEAR

    text = (
        "Week 3 HW3 Due: Oct 12 "
        "Week 4 HW4 due date October 20 "
        "HW5 is due 5PM Thurs Nov 2"
    )
    rows = parse_weekly_schedule_text(text, "COURSE 101")
    by_task = {r["Task"]: r for r in rows}
    assert str(by_task["HW3"]["Due Date"]).startswith(f"{TERM_YEAR}-10-12")
    assert "17:00" not in str(by_task["HW3"]["Due Date"])
    assert str(by_task["HW4"]["Due Date"]).startswith(f"{TERM_YEAR}-10-20")
    assert str(by_task["HW5"]["Due Date"]).startswith(f"{TERM_YEAR}-11-02")
    assert str(by_task["HW5"]["Due Date"]).endswith("17:00")


def test_weekly_parser_does_not_invent_lab_quiz_dates():
    assert parse_weekly_schedule_text("Lab Quiz 1 Sept 15", "COURSE 202") == []


def test_parse_single_pdf_skips_empty_text_and_does_not_call_gemini(tmp_path, monkeypatch, caplog):
    import syllasync.syllabus_module as syllabus_module

    pdf = tmp_path / "COURSE101_Syllabus.pdf"
    pdf.write_bytes(b"%PDF-1.4 empty")
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text", lambda _p: "")
    called = {"gemini": 0}

    def should_not_call(client, prompt):
        called["gemini"] += 1
        raise AssertionError("Gemini should not be called on empty text")

    monkeypatch.setattr(syllabus_module, "_gemini_generate", should_not_call)
    syllabus_module.SYLLABUS_PARSE_ISSUES.clear()

    with caplog.at_level(logging.WARNING, logger="syllasync.syllabus_module"):
        rows = _parse_single_pdf(pdf, client=object())

    assert rows == []
    assert called["gemini"] == 0
    assert "likely a scanned" in caplog.text.lower() or "scanned" in caplog.text.lower()
    assert "Gemini was not called" in caplog.text
    assert any(item["file"] == pdf.name for item in SYLLABUS_PARSE_ISSUES)


def test_parse_single_pdf_skips_near_empty_text(tmp_path, monkeypatch, caplog):
    import syllasync.syllabus_module as syllabus_module

    pdf = tmp_path / "COURSE101_Syllabus.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text", lambda _p: "1\n2\n3\n")
    monkeypatch.setattr(
        syllabus_module,
        "_gemini_generate",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Gemini")),
    )
    syllabus_module.SYLLABUS_PARSE_ISSUES.clear()

    with caplog.at_level(logging.WARNING, logger="syllasync.syllabus_module"):
        rows = _parse_single_pdf(pdf, client=object())

    assert rows == []
    assert "Gemini was not called" in caplog.text


def test_parse_single_pdf_uses_local_rows_and_skips_gemini(tmp_path, monkeypatch):
    import syllasync.syllabus_module as syllabus_module
    from syllasync.config import TERM_YEAR

    pdf = tmp_path / "COURSE101_Syllabus.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    text = "First midterm exam Friday, October 9, 5:30- 6:30pm"
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text", lambda _p: text)
    monkeypatch.setattr(
        syllabus_module,
        "_gemini_generate",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Gemini should not run")),
    )
    rows = _parse_single_pdf(pdf, client=object())
    dated = [r for r in rows if str(r["Due Date"]).upper() not in {"", "TBD"}]
    assert dated
    assert dated[0]["Due Date"] == f"{TERM_YEAR}-10-09 17:30"


def test_gemini_failure_is_nonfatal_and_mentions_local_count(tmp_path, monkeypatch, caplog):
    import syllasync.syllabus_module as syllabus_module

    pdf = tmp_path / "COURSE202 - Lab Problem Schedule.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    # Usable text, but nothing the weekly parser will match.
    monkeypatch.setattr(
        syllabus_module,
        "_extract_pdf_text",
        lambda _p: "Lab problems are listed by week. See the table on page two for details.",
    )

    class QuotaError(RuntimeError):
        status_code = 429

    def fail(_client, _prompt):
        raise QuotaError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(syllabus_module, "_gemini_generate", fail)
    syllabus_module.SYLLABUS_PARSE_ISSUES.clear()

    with caplog.at_level(logging.ERROR, logger="syllasync.syllabus_module"):
        rows = _parse_single_pdf(pdf, client=object())

    assert rows == []
    assert "Gemini parse failed" in caplog.text
    assert "No local parser rows were available" in caplog.text
    assert any("Gemini failed" in item["reason"] for item in SYLLABUS_PARSE_ISSUES)


def test_ocr_is_used_when_enabled_and_extract_is_empty(tmp_path, monkeypatch):
    import syllasync.syllabus_module as syllabus_module
    from syllasync.config import TERM_YEAR

    pdf = tmp_path / "COURSE101_Syllabus.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(syllabus_module, "SYLLABUS_OCR", True)
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text_pypdf2", lambda _p: "")
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text_pypdfium", lambda _p: "")
    monkeypatch.setattr(
        syllabus_module,
        "_ocr_pdf_text",
        lambda _p: "First midterm exam Friday, October 9, 5:30- 6:30pm",
    )
    monkeypatch.setattr(
        syllabus_module,
        "_gemini_generate",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Gemini")),
    )
    rows = _parse_single_pdf(pdf, client=object())
    dated = [r for r in rows if str(r["Due Date"]).upper() not in {"", "TBD"}]
    assert dated and dated[0]["Due Date"] == f"{TERM_YEAR}-10-09 17:30"


def test_ocr_missing_deps_do_not_break_default_path(tmp_path, monkeypatch, caplog):
    import builtins

    import syllasync.syllabus_module as syllabus_module

    pdf = tmp_path / "COURSE101_Syllabus.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(syllabus_module, "SYLLABUS_OCR", True)
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text_pypdf2", lambda _p: "")
    monkeypatch.setattr(syllabus_module, "_extract_pdf_text_pypdfium", lambda _p: "")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pdf2image", "pytesseract"}:
            raise ImportError("missing optional OCR dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    syllabus_module.SYLLABUS_PARSE_ISSUES.clear()

    with caplog.at_level(logging.WARNING, logger="syllasync.syllabus_module"):
        rows = _parse_single_pdf(pdf, client=None)

    assert rows == []
    assert "not installed" in caplog.text
    assert "likely a scanned" in caplog.text.lower() or "scanned" in caplog.text.lower()
    assert "Gemini was not called" in caplog.text


def test_normalize_pdf_text_repairs_ligatures_and_glued_dates():
    from syllasync.syllabus_module import _normalize_pdf_text

    glued = "Midterm 30% Oct24,12:30-2:00PM FinalExam 50% TBA"
    cleaned = _normalize_pdf_text(glued)
    assert "Oct 24" in cleaned
    assert "Final Exam" in cleaned
    assert "2:00 PM" in cleaned or "2:00PM" not in cleaned.replace(" ", "")
    ligature = "There will be a \ufb01nal examination in December."
    assert "final examination" in _normalize_pdf_text(ligature).lower()


def test_exam_parser_reads_glued_midterm_typography():
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import parse_syllabus_exam_text

    text = "Marking Scheme Midterm 30% Oct24,12:30-2:00PM FinalExam 50% TBA"
    rows = parse_syllabus_exam_text(text, "COURSE 101")
    by_task = {r["Task"]: r for r in rows}
    assert str(by_task["Midterm"]["Due Date"]).startswith(f"{TERM_YEAR}-10-24")
    assert "12:30" in str(by_task["Midterm"]["Due Date"])
    assert by_task["Final Exam"]["Due Date"] in {"", "TBD", "TBA"} or str(
        by_task["Final Exam"]["Due Date"]
    ).upper() in {"TBD", "TBA", ""}


def test_due_labeled_items_day_first_and_closes_on():
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import parse_due_labeled_items

    text = (
        "Assignment #1 Complete the form. Due: 16 September 2024 "
        "Assignment #2 Attend a session. Due: sessions run through the term "
        "but no later than December 11 "
        "Assignment #3 Apply online. Due: December 11 at 11:59pm "
        "Quiz #1 opens in the portal. You need to score 100%. "
        "Quiz closes on December 11 at 11:59pm "
        "Quiz #2 opens later. Quiz closes on December 11 at 11:59pm "
        "Quiz #3 opens last. Quiz closes on December 11 at 11:59pm"
    )
    rows = parse_due_labeled_items(text, "COURSE 101")
    by_task = {r["Task"]: r for r in rows}
    assert str(by_task["Assignment #1"]["Due Date"]).startswith(f"{TERM_YEAR}-09-16")
    assert str(by_task["Assignment #2"]["Due Date"]).startswith(f"{TERM_YEAR}-12-11")
    assert str(by_task["Assignment #3"]["Due Date"]).startswith(f"{TERM_YEAR}-12-11")
    assert str(by_task["Assignment #3"]["Due Date"]).endswith("23:59")
    assert str(by_task["Quiz #1"]["Due Date"]).endswith("23:59")
    assert str(by_task["Quiz #2"]["Due Date"]).startswith(f"{TERM_YEAR}-12-11")
    assert str(by_task["Quiz #3"]["Due Date"]).startswith(f"{TERM_YEAR}-12-11")


def test_lab_quiz_schedule_uses_range_end_and_ignores_bare_date():
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import (
        parse_lab_quiz_schedule_text,
        parse_weekly_schedule_text,
    )

    assert parse_lab_quiz_schedule_text("Lab Quiz 1 Sept 15", "COURSE 202") == []
    table = (
        "Lab Number Dates Topics Quiz "
        "1 September 8 - September 11 Topic A No Quiz "
        "3 September 21 - September 25 Topic B Lab Quiz 1 "
        "5 October 5 - October 9 Topic C Lab Quiz 2 "
        "8 October 26 - October 30 Topic D Lab Quiz 3 "
        "10 November 16 - November 20 Topic E Lab Quiz 4 "
        "12 November 30 - December 4 Topic F Lab Quiz 5 "
        "13 Monday, December 7 Topic G No Quiz"
    )
    rows = parse_lab_quiz_schedule_text(table, "COURSE 202")
    by_task = {r["Task"]: r for r in rows}
    assert set(by_task) == {
        "Lab Quiz 1",
        "Lab Quiz 2",
        "Lab Quiz 3",
        "Lab Quiz 4",
        "Lab Quiz 5",
    }
    assert str(by_task["Lab Quiz 1"]["Due Date"]).startswith(f"{TERM_YEAR}-09-25")
    assert str(by_task["Lab Quiz 2"]["Due Date"]).startswith(f"{TERM_YEAR}-10-09")
    assert str(by_task["Lab Quiz 3"]["Due Date"]).startswith(f"{TERM_YEAR}-10-30")
    assert str(by_task["Lab Quiz 4"]["Due Date"]).startswith(f"{TERM_YEAR}-11-20")
    assert str(by_task["Lab Quiz 5"]["Due Date"]).startswith(f"{TERM_YEAR}-12-04")
    # Weekly homework parser still must not invent a quiz date from a bare line.
    assert parse_weekly_schedule_text("Lab Quiz 1 Sept 15", "COURSE 202") == []


def test_lab_quiz_tentative_block_on_syllabus():
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import parse_lab_quiz_schedule_text

    text = (
        "Lab quizzes 15% Tentative Schedule: "
        "September 21 - September 25 "
        "October 5 - October 9 "
        "October 26 - October 30 "
        "Online Assignments 5% Midterm 30% Oct 24"
    )
    rows = parse_lab_quiz_schedule_text(text, "COURSE 202")
    dates = [str(r["Due Date"])[:10] for r in rows]
    assert dates == [
        f"{TERM_YEAR}-09-25",
        f"{TERM_YEAR}-10-09",
        f"{TERM_YEAR}-10-30",
    ]


def test_deferred_exam_wording_variants():
    from syllasync.syllabus_module import parse_syllabus_exam_text

    held = parse_syllabus_exam_text(
        "The deferred exam for this course will be held on Jan. 9, 2027.",
        "COURSE 101",
    )
    by_task = {r["Task"]: r for r in held}
    assert str(by_task["Deferred Final Exam"]["Due Date"]).startswith("2027-01-09")

    saturday = parse_syllabus_exam_text(
        "Deferred final exam: Saturday 09 January 2027, location TBA.",
        "COURSE 101",
    )
    by_task = {r["Task"]: r for r in saturday}
    assert str(by_task["Deferred Final Exam"]["Due Date"]).startswith("2027-01-09")


def test_deterministic_parser_combines_lab_schedule_filename(tmp_path):
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import _parse_with_deterministic_parser

    pdf = tmp_path / "COURSE202_Lab_Problem_Schedule.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    text = (
        "1 September 8 - September 11 Intro No Quiz "
        "3 September 21 - September 25 Review Lab Quiz 1"
    )
    rows = _parse_with_deterministic_parser(pdf, text)
    assert rows is not None
    by_task = {r["Task"]: r for r in rows}
    assert "Lab Quiz 1" in by_task
    assert str(by_task["Lab Quiz 1"]["Due Date"]).startswith(f"{TERM_YEAR}-09-25")

def test_lab_quiz_parser_splits_glued_label():
    from syllasync.config import TERM_YEAR
    from syllasync.syllabus_module import parse_lab_quiz_schedule_text

    text = (
        "1 September 8 - September 11 TopicA No Quiz "
        "5 October 5 - October 9 TopicC ProblemsLab Quiz 2"
    )
    rows = parse_lab_quiz_schedule_text(text, "COURSE 202")
    assert len(rows) == 1
    assert rows[0]["Task"] == "Lab Quiz 2"
    assert str(rows[0]["Due Date"]).startswith(f"{TERM_YEAR}-10-09")

