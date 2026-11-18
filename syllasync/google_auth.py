"""Load Google credentials from credentials.json.

Service-account keys keep the existing path. Installed-app or web OAuth
client secrets use InstalledAppFlow and store a refresh token in
gitignored token.json. OAuth requests only the narrow Sheets and
Calendar scopes already used today (no full Drive scope).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

AUTH_SERVICE_ACCOUNT = "service_account"
AUTH_OAUTH = "oauth"
AUTH_UNKNOWN = "unknown"

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
]
# One OAuth consent covers both APIs so a single token.json is shared.
OAUTH_SCOPES = [*SHEETS_SCOPES, *CALENDAR_SCOPES]

_OAUTH_TYPES = frozenset({"installed", "web"})


def credentials_kind(data: Any) -> str:
    """Classify a credentials.json object without logging secret fields."""
    if not isinstance(data, dict):
        return AUTH_UNKNOWN
    typ = str(data.get("type") or "").strip().lower()
    if typ == AUTH_SERVICE_ACCOUNT:
        return AUTH_SERVICE_ACCOUNT
    if typ in _OAUTH_TYPES:
        return AUTH_OAUTH
    if isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict):
        return AUTH_OAUTH
    return AUTH_UNKNOWN


def oauth_client_block(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the dict that holds client_id / client_secret, if any."""
    typ = str(data.get("type") or "").strip().lower()
    for key in ("installed", "web"):
        block = data.get(key)
        if isinstance(block, dict):
            return block
    if typ in _OAUTH_TYPES:
        return data
    return None


def load_credentials_dict(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("credentials.json must be a JSON object.")
    return data


def _default_paths() -> tuple[Path, Path]:
    from .config import CREDENTIALS_PATH, TOKEN_PATH

    return CREDENTIALS_PATH, TOKEN_PATH


def _run_oauth_flow(credentials_path: Path, scopes: Sequence[str]):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), list(scopes))
    return flow.run_local_server(port=0)


def _refresh_oauth(creds: Any) -> Any:
    from google.auth.transport.requests import Request

    creds.refresh(Request())
    return creds


def _load_saved_token(token_path: Path, scopes: Sequence[str]):
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_file(str(token_path), list(scopes))


def _has_required_scopes(creds: Any, scopes: Sequence[str]) -> bool:
    checker = getattr(creds, "has_scopes", None)
    if callable(checker):
        return bool(checker(list(scopes)))
    saved = getattr(creds, "scopes", None) or []
    return set(scopes) <= set(saved)


def load_oauth_credentials(
    credentials_path: Path,
    token_path: Path,
    scopes: Sequence[str] = OAUTH_SCOPES,
    *,
    flow_fn: Callable[[Path, Sequence[str]], Any] | None = None,
    refresh_fn: Callable[[Any], Any] | None = None,
    token_loader: Callable[[Path, Sequence[str]], Any] | None = None,
) -> Any:
    """Load or refresh user OAuth credentials. Never prints token contents."""
    flow_fn = flow_fn or _run_oauth_flow
    refresh_fn = refresh_fn or _refresh_oauth
    token_loader = token_loader or _load_saved_token

    creds = None
    if token_path.is_file():
        creds = token_loader(token_path, scopes)

    if creds and getattr(creds, "valid", False) and _has_required_scopes(creds, scopes):
        return creds

    if (
        creds
        and getattr(creds, "expired", False)
        and getattr(creds, "refresh_token", None)
        and _has_required_scopes(creds, scopes)
    ):
        creds = refresh_fn(creds)
        _save_token(token_path, creds)
        return creds

    creds = flow_fn(credentials_path, scopes)
    _save_token(token_path, creds)
    return creds


def _save_token(token_path: Path, creds: Any) -> None:
    payload = creds.to_json() if hasattr(creds, "to_json") else json.dumps(creds)
    token_path.write_text(payload, encoding="utf-8")


def load_google_credentials(
    *,
    scopes: Sequence[str],
    credentials_path: Path | None = None,
    token_path: Path | None = None,
    flow_fn: Callable[[Path, Sequence[str]], Any] | None = None,
    refresh_fn: Callable[[Any], Any] | None = None,
    token_loader: Callable[[Path, Sequence[str]], Any] | None = None,
) -> Any:
    """Return credentials for Sheets or Calendar.

    Service-account keys use *scopes* as given (today's per-API behaviour).
    OAuth clients always request OAUTH_SCOPES so one token.json serves both.
    """
    default_creds, default_token = _default_paths()
    credentials_path = credentials_path or default_creds
    token_path = token_path or default_token
    if not credentials_path.is_file():
        raise FileNotFoundError(
            f"credentials.json not found at {credentials_path}. "
            "Use a service-account key or a Desktop OAuth client secrets file."
        )

    data = load_credentials_dict(credentials_path)
    kind = credentials_kind(data)
    if kind == AUTH_SERVICE_ACCOUNT:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=list(scopes)
        )
    if kind == AUTH_OAUTH:
        return load_oauth_credentials(
            credentials_path,
            token_path,
            OAUTH_SCOPES,
            flow_fn=flow_fn,
            refresh_fn=refresh_fn,
            token_loader=token_loader,
        )
    raise ValueError(
        "credentials.json must be a service-account key (type service_account) "
        "or an installed-app / web OAuth client secrets file."
    )
