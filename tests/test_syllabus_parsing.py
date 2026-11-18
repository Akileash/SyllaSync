"""Syllabus PDF text quality, Due: variants, and Gemini fallback behaviour."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from syllasync.syllabus_module import (
    is_usable_pdf_text,
    parse_weekly_schedule_text,
    _parse_single_pdf,
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
