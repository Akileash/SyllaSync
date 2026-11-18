"""Tests for the first-run setup wizard (temp dirs only; no real secrets)."""

from __future__ import annotations

import json
from pathlib import Path

from syllasync.setup_wizard import (
    collect_values,
    format_env_value,
    parse_env,
    read_client_email,
    run_setup,
    upsert_env,
)

SECRET = "test-only-canvas-token-value"
WEBHOOK = "https://discord.com/api/webhooks/1/test-only-webhook-secret"


def test_parse_env_skips_comments_and_unquotes():
    text = (
        "# CANVAS_TOKEN=ignored\n"
        "CANVAS_URL=https://school.example.edu\n"
        'GOOGLE_SHEET_ID="abc 123"\n'
        "\n"
        "export LOCAL_TIMEZONE=America/Edmonton\n"
    )
    values = parse_env(text)
    assert values["CANVAS_URL"] == "https://school.example.edu"
    assert values["GOOGLE_SHEET_ID"] == "abc 123"
    assert values["LOCAL_TIMEZONE"] == "America/Edmonton"
    assert "CANVAS_TOKEN" not in values


def test_upsert_env_updates_and_comments_optional():
    template = (
        "CANVAS_URL=https://old.example.edu\n"
        "# GEMINI_API_KEY=\n"
        "ALLOWED_COURSES=COURSE 101\n"
    )
    out = upsert_env(
        template,
        {
            "CANVAS_URL": "https://school.example.edu",
            "GEMINI_API_KEY": "",
            "TERM_YEAR": "2026",
        },
    )
    assert "CANVAS_URL=https://school.example.edu" in out
    assert "# GEMINI_API_KEY=" in out
    assert "ALLOWED_COURSES=COURSE 101" in out
    assert "TERM_YEAR=2026" in out


def test_format_env_value_quotes_spaces():
    assert format_env_value("plain") == "plain"
    assert format_env_value("has space") == '"has space"'


def test_read_client_email_returns_email_only(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "sa@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\\nTESTKEY\\n-----END PRIVATE KEY-----\\n",
            }
        ),
        encoding="utf-8",
    )
    assert read_client_email(path) == "sa@example.iam.gserviceaccount.com"
    assert read_client_email(tmp_path / "missing.json") is None


def test_collect_values_keeps_secret_without_printing():
    printed: list[str] = []
    answers = iter(["y"] * 9)

    def input_fn(_prompt: str) -> str:
        return next(answers)

    def secret_fn(_prompt: str) -> str:
        raise AssertionError("secret prompt should not run when keeping the value")

    existing = {
        "CANVAS_URL": "https://school.example.edu",
        "CANVAS_TOKEN": SECRET,
        "GOOGLE_SHEET_ID": "sheet-id-123",
        "GOOGLE_CALENDAR_ID": "user@school.edu",
        "DISCORD_WEBHOOK_URL": WEBHOOK,
        "GEMINI_API_KEY": "test-only-gemini-key",
        "LOCAL_TIMEZONE": "America/Edmonton",
        "TERM_YEAR": "2026",
        "SYNC_GRADES": "1",
    }

    result = collect_values(
        existing,
        input_fn=input_fn,
        secret_fn=secret_fn,
        print_fn=printed.append,
    )
    assert result["CANVAS_TOKEN"] == SECRET
    assert result["CANVAS_URL"] == "https://school.example.edu"
    joined = "\n".join(printed)
    assert SECRET not in joined
    assert "CANVAS_TOKEN is already set (value hidden)." in joined
    assert "https://school.example.edu" in joined


def _answers(*values: str):
    remaining = list(values)

    def input_fn(_prompt: str) -> str:
        if not remaining:
            raise AssertionError(f"unexpected prompt: {_prompt!r}")
        return remaining.pop(0)

    return input_fn


def test_run_setup_creates_env_without_printing_secrets(tmp_path, capsys):
    repo = tmp_path
    examples = repo / "examples"
    examples.mkdir()
    (examples / ".env.example").write_text(
        "CANVAS_URL=https://your-university.instructure.com\n"
        "CANVAS_TOKEN=your_canvas_api_token\n"
        "GOOGLE_SHEET_ID=your_google_sheet_id\n"
        "GOOGLE_CALENDAR_ID=your_email@gmail.com\n"
        "# GEMINI_API_KEY=your_gemini_api_key\n",
        encoding="utf-8",
    )
    (examples / "schedule.example.json").write_text('{"term": "Fall", "sections": []}\n', encoding="utf-8")
    # token, then optional discord + gemini (blank to skip)
    secrets = iter([SECRET, "", ""])

    def secret_fn(_prompt: str) -> str:
        return next(secrets)

    # URL, sheet, calendar, timezone (default), term (skip), grades (default Y)
    input_fn = _answers(
        "https://school.example.edu",
        "sheet-id-123",
        "user@example.edu",
        "",
        "",
        "",
    )
    code = run_setup(repo, input_fn=input_fn, secret_fn=secret_fn)
    assert code == 0
    text = (repo / ".env").read_text(encoding="utf-8")
    assert "CANVAS_URL=https://school.example.edu" in text
    assert f"CANVAS_TOKEN={SECRET}" in text
    assert "GOOGLE_SHEET_ID=sheet-id-123" in text
    assert "GOOGLE_CALENDAR_ID=user@example.edu" in text
    assert "LOCAL_TIMEZONE=America/Edmonton" in text
    assert "SYNC_GRADES=1" in text
    assert "# GEMINI_API_KEY=" in text or "GEMINI_API_KEY=" not in text.split("# GEMINI_API_KEY")[0]
    assert (repo / "schedule.json").is_file()
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "Wrote .env" in out
    assert "python main.py --check --all" in out


def test_run_setup_keeps_or_overwrites_existing_fields(tmp_path, capsys):
    repo = tmp_path
    (repo / ".env").write_text(
        f"CANVAS_URL=https://old.example.edu\nCANVAS_TOKEN={SECRET}\n"
        "GOOGLE_SHEET_ID=old-sheet\nGOOGLE_CALENDAR_ID=old@example.edu\n"
        f"DISCORD_WEBHOOK_URL={WEBHOOK}\nLOCAL_TIMEZONE=America/Edmonton\n"
        "TERM_YEAR=2026\nSYNC_GRADES=1\n",
        encoding="utf-8",
    )
    credentials = {"type": "service_account", "private_key": "KEEP-ME", "client_email": "sa@example.iam.gserviceaccount.com"}
    (repo / "credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    (repo / "schedule.json").write_text('{"term": "keep"}\n', encoding="utf-8")
    new_token = "test-only-replacement-token"
    secrets = iter([new_token, ""])  # replace token; skip unset Gemini key

    def secret_fn(_prompt: str) -> str:
        return next(secrets)

    # keep url, replace token, keep sheet, calendar, webhook, tz, term, grades
    input_fn = _answers("y", "n", "y", "y", "y", "y", "y", "y")
    code = run_setup(repo, input_fn=input_fn, secret_fn=secret_fn)
    assert code == 0
    text = (repo / ".env").read_text(encoding="utf-8")
    assert "CANVAS_URL=https://old.example.edu" in text
    assert f"CANVAS_TOKEN={new_token}" in text
    assert SECRET not in text
    assert "GOOGLE_SHEET_ID=old-sheet" in text
    assert f"DISCORD_WEBHOOK_URL={WEBHOOK}" in text
    out = capsys.readouterr().out
    assert SECRET not in out
    assert new_token not in out
    assert WEBHOOK not in out
    assert "sa@example.iam.gserviceaccount.com" in out
    # credentials.json and existing schedule.json must be untouched
    saved = json.loads((repo / "credentials.json").read_text(encoding="utf-8"))
    assert saved == credentials
    assert (repo / "schedule.json").read_text(encoding="utf-8") == '{"term": "keep"}\n'


def test_run_setup_does_not_create_credentials(tmp_path):
    repo = tmp_path
    input_fn = _answers(
        "https://school.example.edu",
        "sheet-id",
        "cal-id",
        "",
        "",
        "n",
    )
    secrets = iter(["test-only-token", "", ""])

    def secret_fn(_prompt: str) -> str:
        return next(secrets)
    run_setup(repo, input_fn=input_fn, secret_fn=secret_fn, print_fn=lambda _m: None)
    assert (repo / ".env").is_file()
    assert "SYNC_GRADES=0" in (repo / ".env").read_text(encoding="utf-8")
    assert not (repo / "credentials.json").exists()
