"""Google auth selection: service-account vs installed-app OAuth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syllasync.config import collect_config_issues, format_config_report
from syllasync.google_auth import (
    AUTH_OAUTH,
    AUTH_SERVICE_ACCOUNT,
    AUTH_UNKNOWN,
    CALENDAR_SCOPES,
    OAUTH_SCOPES,
    SHEETS_SCOPES,
    credentials_kind,
    load_google_credentials,
    load_oauth_credentials,
    oauth_client_block,
)

FAKE_SA = {
    "type": "service_account",
    "project_id": "example-project",
    "private_key_id": "fake-key-id",
    "private_key": "-----BEGIN PRIVATE KEY-----\\nTEST-ONLY-FAKE-KEY\\n-----END PRIVATE KEY-----\\n",
    "client_email": "sa@example.iam.gserviceaccount.com",
    "client_id": "100000000000000000000",
    "token_uri": "https://oauth2.googleapis.com/token",
}

FAKE_INSTALLED = {
    "installed": {
        "client_id": "100000000000-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com",
        "project_id": "example-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "test-only-oauth-client-secret",
        "redirect_uris": ["http://localhost"],
    }
}

FAKE_WEB = {
    "web": {
        "client_id": "100000000000-webclient.apps.googleusercontent.com",
        "project_id": "example-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "test-only-web-client-secret",
        "redirect_uris": ["http://localhost"],
    }
}


class FakeCreds:
    def __init__(
        self,
        *,
        valid: bool = True,
        expired: bool = False,
        refresh_token: str | None = "test-only-refresh-token",
        scopes: list[str] | None = None,
        token: str = "test-only-access-token",
    ):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.scopes = list(scopes or OAUTH_SCOPES)
        self.token = token
        self.refresh_calls = 0

    def has_scopes(self, scopes: list[str]) -> bool:
        return set(scopes) <= set(self.scopes)

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "scopes": self.scopes,
            }
        )


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_credentials_kind_detects_service_account():
    assert credentials_kind(FAKE_SA) == AUTH_SERVICE_ACCOUNT


def test_credentials_kind_detects_installed_and_web_wrappers():
    assert credentials_kind(FAKE_INSTALLED) == AUTH_OAUTH
    assert credentials_kind(FAKE_WEB) == AUTH_OAUTH
    assert oauth_client_block(FAKE_INSTALLED)["client_id"].endswith("googleusercontent.com")
    assert oauth_client_block(FAKE_WEB)["client_secret"] == "test-only-web-client-secret"


def test_credentials_kind_detects_top_level_installed_or_web_type():
    installed = {
        "type": "installed",
        "client_id": "desktop-client-id.apps.googleusercontent.com",
        "client_secret": "test-only-desktop-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    web = {
        "type": "web",
        "client_id": "web-client-id.apps.googleusercontent.com",
        "client_secret": "test-only-top-level-web-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    assert credentials_kind(installed) == AUTH_OAUTH
    assert credentials_kind(web) == AUTH_OAUTH
    assert oauth_client_block(installed)["client_id"] == installed["client_id"]


def test_credentials_kind_unknown_for_junk():
    assert credentials_kind({"hello": "world"}) == AUTH_UNKNOWN
    assert credentials_kind([1, 2, 3]) == AUTH_UNKNOWN
    assert credentials_kind(None) == AUTH_UNKNOWN


def test_oauth_scopes_are_narrow_and_cover_sheets_and_calendar():
    assert SHEETS_SCOPES == ["https://www.googleapis.com/auth/spreadsheets"]
    assert "https://www.googleapis.com/auth/drive" not in SHEETS_SCOPES
    assert "https://www.googleapis.com/auth/drive" not in CALENDAR_SCOPES
    assert "https://www.googleapis.com/auth/drive" not in OAUTH_SCOPES
    assert list(SHEETS_SCOPES) == list(
        __import__("syllasync.google_sheets_module", fromlist=["SCOPES"]).SCOPES
    )
    assert list(CALENDAR_SCOPES) == list(
        __import__("syllasync.google_calendar_module", fromlist=["SCOPES"]).SCOPES
    )
    for scope in SHEETS_SCOPES + CALENDAR_SCOPES:
        assert scope in OAUTH_SCOPES


def test_check_accepts_service_account_and_installed_client(monkeypatch, tmp_path):
    sa_path = _write(tmp_path / "sa.json", FAKE_SA)
    oauth_path = _write(tmp_path / "oauth.json", FAKE_INSTALLED)
    monkeypatch.setattr("syllasync.config.CANVAS_URL", "https://school.example.edu")
    monkeypatch.setattr("syllasync.config.CANVAS_TOKEN", "test-only-canvas-token")
    monkeypatch.setattr("syllasync.config.GOOGLE_SHEET_ID", "sheet-id-123")
    monkeypatch.setattr("syllasync.config.GOOGLE_CALENDAR_ID", "user@school.edu")
    monkeypatch.setattr("syllasync.config.DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr("syllasync.config.GEMINI_API_KEY", "")
    monkeypatch.setattr("syllasync.config.SCHEDULE_PATH", tmp_path / "schedule.json")
    monkeypatch.setattr("syllasync.config.LOCAL_TIMEZONE", "America/Edmonton")
    monkeypatch.setattr("syllasync.config.TOKEN_PATH", tmp_path / "token.json")

    monkeypatch.setattr("syllasync.config.CREDENTIALS_PATH", sa_path)
    sa_issues = collect_config_issues(require_google=True, require_calendar=True)
    sa_errors = [item for item in sa_issues if item.level == "error" and item.name == "credentials.json"]
    assert sa_errors == []

    monkeypatch.setattr("syllasync.config.CREDENTIALS_PATH", oauth_path)
    oauth_issues = collect_config_issues(require_google=True, require_calendar=True)
    oauth_errors = [item for item in oauth_issues if item.level == "error" and item.name == "credentials.json"]
    assert oauth_errors == []
    oauth_warns = [item for item in oauth_issues if item.level == "warn" and item.name == "token.json"]
    assert oauth_warns
    assert "test user" in oauth_warns[0].message.lower() or "browser" in oauth_warns[0].message.lower()
    report = format_config_report(oauth_issues)
    assert "test-only-oauth-client-secret" not in report
    assert "100000000000-abcdefghijklmnopqrstuvwxyz" not in report


def test_check_rejects_oauth_client_missing_secret(monkeypatch, tmp_path):
    data = {"installed": {"client_id": "desktop-id.apps.googleusercontent.com", "token_uri": "https://oauth2.googleapis.com/token"}}
    path = _write(tmp_path / "oauth.json", data)
    monkeypatch.setattr("syllasync.config.CREDENTIALS_PATH", path)
    monkeypatch.setattr("syllasync.config.TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr("syllasync.config.CANVAS_URL", "https://school.example.edu")
    monkeypatch.setattr("syllasync.config.CANVAS_TOKEN", "test-only-canvas-token")
    monkeypatch.setattr("syllasync.config.GOOGLE_SHEET_ID", "sheet-id-123")
    monkeypatch.setattr("syllasync.config.GOOGLE_CALENDAR_ID", "user@school.edu")
    monkeypatch.setattr("syllasync.config.SCHEDULE_PATH", tmp_path / "schedule.json")
    monkeypatch.setattr("syllasync.config.LOCAL_TIMEZONE", "America/Edmonton")
    issues = collect_config_issues(require_google=True, require_calendar=True)
    messages = [item.message for item in issues if item.name == "credentials.json"]
    assert any("client_secret" in message for message in messages)
    joined = "\n".join(messages)
    assert "desktop-id.apps.googleusercontent.com" not in joined


def test_load_google_credentials_uses_service_account_file(monkeypatch, tmp_path):
    creds_path = _write(tmp_path / "credentials.json", FAKE_SA)
    seen: dict[str, object] = {}

    class _SA:
        @staticmethod
        def from_service_account_file(filename, scopes=None):
            seen["filename"] = filename
            seen["scopes"] = list(scopes)
            return "sa-creds"

    import google.oauth2.service_account as sa_mod

    monkeypatch.setattr(sa_mod, "Credentials", _SA)

    result = load_google_credentials(
        scopes=SHEETS_SCOPES,
        credentials_path=creds_path,
        token_path=tmp_path / "token.json",
    )
    assert result == "sa-creds"
    assert seen["filename"] == str(creds_path)
    assert seen["scopes"] == list(SHEETS_SCOPES)
    assert not (tmp_path / "token.json").exists()


def test_load_oauth_runs_mocked_flow_when_token_missing(tmp_path):
    creds_path = _write(tmp_path / "credentials.json", FAKE_INSTALLED)
    token_path = tmp_path / "token.json"
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def flow_fn(path: Path, scopes) -> FakeCreds:
        calls.append((path, tuple(scopes)))
        return FakeCreds()

    def should_not_refresh(_creds):
        raise AssertionError("refresh should not run when no token is saved")

    result = load_google_credentials(
        scopes=SHEETS_SCOPES,
        credentials_path=creds_path,
        token_path=token_path,
        flow_fn=flow_fn,
        refresh_fn=should_not_refresh,
    )
    assert isinstance(result, FakeCreds)
    assert calls == [(creds_path, tuple(OAUTH_SCOPES))]
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["token"] == "test-only-access-token"
    assert saved["refresh_token"] == "test-only-refresh-token"


def test_load_oauth_reuses_valid_token_without_flow(tmp_path):
    creds_path = _write(tmp_path / "credentials.json", FAKE_INSTALLED)
    token_path = tmp_path / "token.json"
    existing = FakeCreds()

    def token_loader(path: Path, scopes) -> FakeCreds:
        assert path == token_path
        assert list(scopes) == list(OAUTH_SCOPES)
        return existing

    token_path.write_text(existing.to_json(), encoding="utf-8")

    def should_not_flow(path, scopes):
        raise AssertionError("OAuth flow should not run when a valid token exists")

    result = load_oauth_credentials(
        creds_path,
        token_path,
        OAUTH_SCOPES,
        flow_fn=should_not_flow,
        token_loader=token_loader,
    )
    assert result is existing


def test_load_oauth_refreshes_expired_token_without_flow(tmp_path):
    creds_path = _write(tmp_path / "credentials.json", FAKE_INSTALLED)
    token_path = tmp_path / "token.json"
    expired = FakeCreds(valid=False, expired=True)

    def token_loader(path: Path, scopes) -> FakeCreds:
        return expired

    def refresh_fn(creds: FakeCreds) -> FakeCreds:
        creds.refresh_calls += 1
        creds.valid = True
        creds.expired = False
        creds.token = "test-only-refreshed-access-token"
        return creds

    def should_not_flow(path, scopes):
        raise AssertionError("OAuth flow should not run when a refresh token exists")

    token_path.write_text(expired.to_json(), encoding="utf-8")
    result = load_oauth_credentials(
        creds_path,
        token_path,
        OAUTH_SCOPES,
        flow_fn=should_not_flow,
        refresh_fn=refresh_fn,
        token_loader=token_loader,
    )
    assert result is expired
    assert expired.refresh_calls == 1
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["token"] == "test-only-refreshed-access-token"


def test_load_google_credentials_rejects_unknown_type(tmp_path):
    creds_path = _write(tmp_path / "credentials.json", {"hello": "world"})
    with pytest.raises(ValueError, match="service-account"):
        load_google_credentials(
            scopes=SHEETS_SCOPES,
            credentials_path=creds_path,
            token_path=tmp_path / "token.json",
            flow_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("flow")),
        )
