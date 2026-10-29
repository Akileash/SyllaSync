"""Encoding, path, and redaction helpers that must not leak secrets."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_WEBHOOK_RE = re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(token|key|api_key|access_token|refresh_token|webhook)=([^&\s]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def configure_stdio() -> None:
    """
    Force UTF-8 console output.

    Windows consoles default to a legacy code page and raise UnicodeEncodeError
    on course names or PDF titles. errors='replace' keeps a run alive.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def redact_text(value: object, *, limit: int = 500) -> str:
    """Return a short error string with tokens, webhooks, and keys removed."""
    text = str(value or "")
    text = _PRIVATE_KEY_RE.sub("<redacted-private-key>", text)
    text = _WEBHOOK_RE.sub("https://discord.com/api/webhooks/<redacted>", text)
    text = _GOOGLE_KEY_RE.sub("<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\1=<redacted>", text)
    for name in ("CANVAS_TOKEN", "GEMINI_API_KEY", "DISCORD_WEBHOOK_URL", "GOOGLE_SHEET_ID"):
        secret = os.environ.get(name) or ""
        if len(secret) >= 8:
            text = text.replace(secret, "<redacted>")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def is_google_auth_failure(exc: BaseException) -> bool:
    """True for service-account key / refresh failures (not a sheet permission)."""
    name = type(exc).__name__
    if name in {"RefreshError", "MalformedError", "DefaultCredentialsError"}:
        return True
    text = str(exc).lower()
    return ("invalid_grant" in text) or ("refresh" in text and "token" in text)


def explain_google_auth_failure(exc: BaseException) -> str:
    """Actionable Google auth message. Never includes the private key."""
    if type(exc).__name__ in {"JSONDecodeError", "UnicodeError"} or isinstance(
        exc, (json.JSONDecodeError, UnicodeError, ValueError)
    ):
        return (
            "credentials.json could not be read. Download a new service-account "
            "JSON key from Google Cloud Console (Sheets API and Calendar API enabled) "
            "and replace credentials.json. The file contents were not logged."
        )
    return (
        "Google rejected the service-account credentials while refreshing an access "
        "token. The key may be revoked, the system clock may be wrong, or the "
        "private key is incomplete. Download a new JSON key. "
        f"Details: {redact_text(exc)}"
    )


def clean_path(value: object) -> str:
    """
    Normalize a user-supplied path.

    Strips UTF-8 BOM, wrapping quotes (common when a OneDrive path is pasted
    into .env), and expands a leading ~.
    """
    text = str(value or "").replace("\ufeff", "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if not text:
        return ""
    return str(Path(text).expanduser())


def read_text(path: Path) -> str:
    """Read UTF-8 text, accepting a BOM and Windows CRLF newlines."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnicodeError(
            f"Could not read {path.name} as UTF-8. Save the file as UTF-8 "
            "(Windows Notepad: UTF-8, not UTF-16). The file contents were not logged."
        ) from exc


def read_json(path: Path) -> Any:
    """Load JSON saved as UTF-8, with or without a BOM, with LF or CRLF."""
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(
            f"{path.name} is not valid JSON ({exc.msg}). "
            "If this file was edited on Windows, re-save it as UTF-8.",
            exc.doc,
            exc.pos,
        ) from exc
