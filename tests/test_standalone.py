"""Offline checks for config validation, redaction, retries, and malformed model output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from syllasync.config import collect_config_issues, format_config_report, is_placeholder
from syllasync.cli import calendar_prune_enabled
from syllasync.retry_utils import is_retryable, with_retries
from syllasync.safe_text import clean_path, read_json, redact_text
from syllasync.syllabus_module import parse_model_payload
from syllasync.vocab import excel_status, sheet_status

ROOT = Path(__file__).resolve().parents[1]


def test_sheet_and_excel_never_write_complete():
    samples = [
        "Complete",
        "completed",
        "done",
        "Done",
        "graded",
        "Graded",
        "submitted",
        "cancelled",
        "Not Started",
        "in progress",
        "",
        None,
    ]
    for sample in samples:
        for label in (sheet_status(sample), excel_status(sample)):
            assert label.lower() not in {"complete", "completed"}


def test_masterlist_status_cell_is_submitted_for_legacy_complete():
    from syllasync.google_sheets_module import _internal_to_masterlist_row

    row = _internal_to_masterlist_row(
        {
            "Course": "COURSE 101",
            "Assessment title": "HW1",
            "Due Date": "2026-10-01 17:00",
            "Status": "Complete",
            "Task_ID": "canvas_1",
        }
    )
    assert row is not None
    assert row[1] == "Submitted"

    graded = _internal_to_masterlist_row(
        {
            "Course": "COURSE 101",
            "Assessment title": "HW1",
            "Due Date": "2026-10-01 17:00",
            "Status": "Graded",
            "Task_ID": "canvas_1",
        }
    )
    assert graded is not None
    assert graded[1] == "Submitted"

    done = _internal_to_masterlist_row(
        {
            "Course": "COURSE 101",
            "Assessment title": "HW1",
            "Due Date": "2026-10-01 17:00",
            "Status": "Done",
            "Task_ID": "canvas_1",
        }
    )
    assert done is not None
    assert done[1] == "Submitted"


def test_finished_statuses_tie_so_sheet_value_wins():
    from syllasync.vocab import SHEET_STATUS_LABELS, choose_status, preserve_sheet_label, status_rank

    assert "Submitted" in SHEET_STATUS_LABELS
    assert preserve_sheet_label("Graded") == "Submitted"
    assert preserve_sheet_label("Complete") == "Submitted"
    assert preserve_sheet_label("Done") == "Submitted"
    assert status_rank("Graded") == status_rank("Submitted")
    assert status_rank("Done") == status_rank("Complete") == status_rank("Cancelled")
    assert choose_status("Graded", "Submitted") == "Submitted"
    assert choose_status("Submitted", "Graded") == "Submitted"
    assert choose_status("Submitted", "Not Started") == "Submitted"
    assert choose_status("Not Started", "Done") == "Submitted"
    assert choose_status("In Progress", "Not Started") == "In Progress"
    for label in (
        choose_status("Graded", "Complete"),
        choose_status("Complete", "Graded"),
        choose_status("", "completed"),
        choose_status("Done", "Submitted"),
    ):
        assert label.lower() not in {"complete", "completed"}


def test_merge_keeps_typed_graded_over_synced_submitted(tmp_path, monkeypatch):
    import pandas as pd

    from syllasync.dedupe_module import TASK_ID_COL
    from syllasync.sheets_module import merge_tracker

    monkeypatch.setattr("syllasync.dedupe_module.STATE_PATH", tmp_path / "state.json")
    existing = pd.DataFrame(
        [
            {
                "Course": "COURSE 101",
                "Assessment title": "HW1",
                "Due Date": "2026-10-01",
                "Estimated time dedicated to task": "",
                "Status": "Graded",
                "Priority": "",
                TASK_ID_COL: "canvas_1",
                "Is Draft": False,
            }
        ]
    )
    incoming = pd.DataFrame(
        [
            {
                "Course": "COURSE 101",
                "Assessment title": "HW1",
                "Due Date": "2026-10-01",
                TASK_ID_COL: "canvas_1",
                "Is Draft": False,
                "Status": "Submitted",
                "Source": "Canvas",
            }
        ]
    )
    merged = merge_tracker(existing, incoming)
    assert merged.iloc[0]["Status"] == "Graded"


def test_tzdata_age_check():
    from syllasync.config import MIN_TZDATA_VERSION, tzdata_is_old

    assert MIN_TZDATA_VERSION == "2026.5"
    assert tzdata_is_old("2025.1") is True
    assert tzdata_is_old("2026.4") is True
    assert tzdata_is_old("2026.5") is False
    assert tzdata_is_old("2026.10") is False
    assert tzdata_is_old("") is True


def test_edmonton_stays_utc_minus_6_after_november_2026():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 2 Nov 2026 is after the old DST fallback. Alberta no longer leaves UTC-6.
    moment = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo("America/Edmonton"))
    offset = moment.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == -6 * 3600


def test_sheet_probe_is_read_only_unless_write(monkeypatch):
    import importlib.util
    _ts = ROOT / "scripts" / "test_sheets.py"
    _spec = importlib.util.spec_from_file_location("test_sheets", _ts)
    test_sheets = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(test_sheets)

    monkeypatch.setattr(sys, "argv", ["test_sheets.py"])
    assert test_sheets.parse_args().write is False
    monkeypatch.setattr(sys, "argv", ["test_sheets.py", "--write"])
    assert test_sheets.parse_args().write is True


def test_placeholder_detection_does_not_require_the_secret_value():
    assert is_placeholder("your_canvas_api_token") is True
    assert is_placeholder("https://your-university.instructure.com") is True
    assert is_placeholder("https://discord.com/api/webhooks/your_webhook_id/your_webhook_token")
    assert is_placeholder("") is False
    assert is_placeholder("a-real-looking-token") is False


def test_config_report_never_prints_secret_values(monkeypatch, tmp_path):
    secret = "super-secret-canvas-token-value"
    webhook = "https://discord.com/api/webhooks/999/webhook-secret-value"
    monkeypatch.setattr("syllasync.config.CANVAS_URL", "https://school.example.edu")
    monkeypatch.setattr("syllasync.config.CANVAS_TOKEN", secret)
    monkeypatch.setattr("syllasync.config.DISCORD_WEBHOOK_URL", webhook)
    monkeypatch.setattr("syllasync.config.GOOGLE_SHEET_ID", "your_google_sheet_id")
    monkeypatch.setattr("syllasync.config.GOOGLE_CALENDAR_ID", "")
    monkeypatch.setattr("syllasync.config.GEMINI_API_KEY", "your_gemini_api_key")
    monkeypatch.setattr("syllasync.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("syllasync.config.SCHEDULE_PATH", tmp_path / "schedule.json")
    monkeypatch.setattr("syllasync.config.LOCAL_TIMEZONE", "America/Edmonton")

    report = format_config_report(
        collect_config_issues(require_google=True, require_discord=True, require_calendar=True)
    )
    assert secret not in report
    assert "webhook-secret-value" not in report
    assert "your_google_sheet_id" not in report
    assert "your_gemini_api_key" not in report
    assert "placeholder" in report.lower()
    assert "[ERROR]" in report


def test_redact_strips_webhook_private_key_and_env_token(monkeypatch):
    monkeypatch.setenv("CANVAS_TOKEN", "canvas-token-should-vanish")
    raw = (
        "401 for url https://discord.com/api/webhooks/12/secret-part "
        "Bearer canvas-token-should-vanish "
        "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
    )
    cleaned = redact_text(raw)
    assert "secret-part" not in cleaned
    assert "canvas-token-should-vanish" not in cleaned
    assert "ABC" not in cleaned
    assert "<redacted>" in cleaned


def test_clean_path_strips_bom_and_quotes():
    cleaned = clean_path('\ufeff"~/assignments.xlsx"')
    assert "\ufeff" not in cleaned
    assert not cleaned.startswith('"')
    assert cleaned.endswith("assignments.xlsx")


def test_read_json_accepts_bom_and_crlf(tmp_path):
    path = tmp_path / "schedule.json"
    payload = '{"term": "Fall", "sections": [{"course": "COURSE 101"}]}\r\n'
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    data = read_json(path)
    assert data["term"] == "Fall"
    assert data["sections"][0]["course"] == "COURSE 101"


def test_schedule_loader_accepts_bom(tmp_path, monkeypatch):
    import syllasync.schedule_module as schedule_module

    path = tmp_path / "schedule.json"
    body = {"term": "Fall 2026", "sections": [{"course": "COURSE 302", "type": "LEC", "section": "A1"}]}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(body).encode("utf-8"))
    monkeypatch.setattr(schedule_module, "SCHEDULE_PATH", path)
    schedule_module.load_schedule.cache_clear()
    loaded = schedule_module.load_schedule()
    schedule_module.load_schedule.cache_clear()
    assert loaded["sections"][0]["section"] == "A1"


def test_gemini_high_demand_is_retried(monkeypatch):
    class HighDemand(RuntimeError):
        status_code = 503

    assert is_retryable(HighDemand("UNAVAILABLE: high demand")) is True
    assert is_retryable(RuntimeError("API key not valid")) is False

    sleeps: list[float] = []
    monkeypatch.setattr("syllasync.retry_utils.time.sleep", lambda delay: sleeps.append(delay))
    calls = {"n": 0}

    @with_retries(max_attempts=3, base_delay=0.01, max_delay=0.05, label="gemini.test")
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise HighDemand("503 high demand")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_parse_model_payload_accepts_fenced_trailing_comma_and_truncated():
    fenced = """Sure:
```json
[{"Course": "COURSE 101", "Task": "HW1", "Due Date": "2026-10-01",}, "skip-me"]
```
"""
    rows = parse_model_payload(fenced)
    assert rows == [
        {
            "Source": "Syllabus",
            "Course": "COURSE 101",
            "Task": "HW1",
            "Due Date": "2026-10-01",
        }
    ]

    truncated = (
        '[{"Course": "COURSE 301", "Task": "Lab 1", "Due Date": "2026-10-02"}, '
        '{"Course": "ECE'
    )
    salvaged = parse_model_payload(truncated)
    assert salvaged[0]["Task"] == "Lab 1"
    assert salvaged[0]["Course"] == "COURSE 301"

    with pytest.raises(ValueError):
        parse_model_payload("I could not find any assignments.")


def test_canvas_error_hides_token():
    from syllasync.canvas_module import explain_canvas_error

    message = explain_canvas_error(RuntimeError("401 invalid access token abc123secret"))
    assert "abc123secret" not in message
    assert "expired" in message.lower() or "revoked" in message.lower()


def test_partial_calendar_sync_does_not_delete_events(monkeypatch):
    """A partial run must not delete events. A full run still may."""
    import pandas as pd

    import syllasync.google_calendar_module as gcal

    deleted: list[str] = []
    existing = [
        {
            "id": "old-event",
            "summary": "COURSE 101 - HW9",
            "task_id": "canvas_old",
            "fuzzy_key": "course 101||homework#9",
            "start_key": "2026-09-01",
            "status": "",
            "priority": "",
            "colorId": "",
        }
    ]
    monkeypatch.setattr(gcal, "_authenticate", lambda: object())
    monkeypatch.setattr(gcal, "GOOGLE_CALENDAR_ID", "calendar@example.com")
    monkeypatch.setattr(
        gcal, "_get_existing_syllasync_events", lambda _service, _calendar_id: existing
    )
    monkeypatch.setattr(
        gcal,
        "_delete_event",
        lambda _service, _calendar_id, event_id: deleted.append(event_id),
    )
    empty = pd.DataFrame(
        columns=[
            "Course",
            "Assessment title",
            "Due Date",
            "Status",
            "Priority",
            "Task_ID",
            "Is Draft",
        ]
    )
    gcal.push_to_google_calendar(empty, dry_run=False, prune=False)
    assert deleted == []

    gcal.push_to_google_calendar(empty, dry_run=True, prune=True)
    assert deleted == []

    gcal.push_to_google_calendar(empty, dry_run=False, prune=True)
    assert deleted == ["old-event"]


def test_calendar_prune_stays_off_when_a_source_failed():
    assert calendar_prune_enabled(canvas_failed=False, sheet_degraded=False) is True
    assert calendar_prune_enabled(canvas_failed=True, sheet_degraded=False) is False
    assert calendar_prune_enabled(canvas_failed=False, sheet_degraded=True) is False


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "CANVAS_URL",
        "CANVAS_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "GEMINI_API_KEY",
        "GOOGLE_SHEET_ID",
        "GOOGLE_CALENDAR_ID",
        "ALLOWED_COURSES",
    ):
        env.pop(name, None)
    env["PYTHONUTF8"] = "1"
    env["SYLLASYNC_SKIP_DOTENV"] = "1"
    return env


def test_check_and_dry_run_with_dummy_config():
    env = _isolated_env()
    env.update(
        {
            "CANVAS_URL": "https://your-university.instructure.com",
            "CANVAS_TOKEN": "your_canvas_api_token",
            "DISCORD_WEBHOOK_URL": (
                "https://discord.com/api/webhooks/your_webhook_id/your_webhook_token"
            ),
            "GOOGLE_SHEET_ID": "your_google_sheet_id",
            "GOOGLE_CALENDAR_ID": "your_email@gmail.com",
        }
    )
    check = subprocess.run(
        [sys.executable, "main.py", "--check", "--all"],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    combined = check.stdout + check.stderr
    assert check.returncode == 1
    assert "your_canvas_api_token" not in combined
    assert "your_webhook_token" not in combined
    assert "placeholder" in combined.lower()

    dry = subprocess.run(
        [sys.executable, "main.py", "--all", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    dry_out = dry.stdout + dry.stderr
    assert dry.returncode == 0, dry_out
    assert "DRY-RUN complete" in dry_out
    assert "your_canvas_api_token" not in dry_out
    assert "your_webhook_token" not in dry_out


def test_check_reports_missing_names_without_a_dotenv_file():
    result = subprocess.run(
        [sys.executable, "main.py", "--validate"],
        cwd=ROOT,
        env=_isolated_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "CANVAS_URL" in combined
    assert "CANVAS_TOKEN" in combined
