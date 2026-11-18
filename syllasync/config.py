"""Centralized environment configuration loaded from .env or local.env."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from dotenv import load_dotenv

from .safe_text import clean_path

BASE_DIR = Path(__file__).resolve().parent.parent

# utf-8-sig strips a BOM so the first key is not "\ufeffCANVAS_URL" on Windows.
if os.getenv("SYLLASYNC_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "yes"}:
    load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")
    load_dotenv(BASE_DIR / "local.env", override=True, encoding="utf-8-sig")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        print(
            f"Warning: {name} must be an integer; using {default}. The value was not logged.",
            file=sys.stderr,
        )
        return default


CANVAS_URL = (os.getenv("CANVAS_URL") or "").strip()
CANVAS_TOKEN = (os.getenv("CANVAS_TOKEN") or "").strip()
DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GOOGLE_SHEET_ID = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
EXCEL_FILE_PATH = clean_path(os.getenv("EXCEL_FILE_PATH", str(BASE_DIR / "assignments.xlsx")))
ALLOWED_COURSES = os.getenv("ALLOWED_COURSES", "")
GOOGLE_CALENDAR_ID = (os.getenv("GOOGLE_CALENDAR_ID") or "").strip()
SCHEDULE_PATH = BASE_DIR / "schedule.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

# Optional Gemini model override (google-genai SDK)
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip()

# Past-due Canvas lookback window (days)
CANVAS_PAST_DUE_DAYS = _env_int("CANVAS_PAST_DUE_DAYS", 21)

# Alberta stays on UTC-6 from November 2026. Older tzdata still applies DST.
MIN_TZDATA_VERSION = "2026.5"

# Read Canvas announcements for exam dates (midterm/final) that only appear
# there. Read-only; set CANVAS_READ_ANNOUNCEMENTS=0 to disable.
CANVAS_READ_ANNOUNCEMENTS = os.getenv("CANVAS_READ_ANNOUNCEMENTS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


# Pull Canvas grades into Masterlist columns K-M. Default on so current
# behaviour is unchanged. Set SYNC_GRADES=0 to skip the grade fetch and
# leave those columns as they are.
SYNC_GRADES = _env_flag("SYNC_GRADES", True)

# Optional OCR for image-only syllabus PDFs. Off by default. Requires
# Tesseract plus the pdf2image and pytesseract packages; missing deps
# are skipped and do not break a normal install.
SYLLABUS_OCR = _env_flag("SYLLABUS_OCR", False)

DEFAULT_SHEET_TAB_NAME = "Masterlist"
DEFAULT_SHEET_META_TAB_NAME = "SyllaSync_Meta"
DEFAULT_SHEET_DATA_START_ROW = 11


def parse_sheet_tab_name(
    value: object, default: str = DEFAULT_SHEET_TAB_NAME
) -> str:
    """Return a non-empty worksheet name. Blank values use *default*."""
    raw = str(value or "").strip()
    return raw or default


def parse_sheet_data_start_row(
    value: object,
    *,
    default: int = DEFAULT_SHEET_DATA_START_ROW,
    warn: bool = False,
) -> int:
    """Return a 1-based start row. Invalid or empty values use *default*."""
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError:
        number = 0
    if number < 1:
        if warn:
            print(
                "Warning: SHEET_DATA_START_ROW must be a positive integer; "
                f"using {default}. The value was not logged.",
                file=sys.stderr,
            )
        return default
    return number


# Default layout matches the current Masterlist template so existing
# workbooks keep working with no .env changes. A simple sheet can set
# SHEET_DATA_START_ROW=2 and put headers in row 1. Columns stay A-H
# and K-M; I-J are never written.
SHEET_TAB_NAME = parse_sheet_tab_name(os.getenv("SHEET_TAB_NAME"))
SHEET_META_TAB_NAME = parse_sheet_tab_name(
    os.getenv("SHEET_META_TAB_NAME"), DEFAULT_SHEET_META_TAB_NAME
)
SHEET_DATA_START_ROW = parse_sheet_data_start_row(
    os.getenv("SHEET_DATA_START_ROW"), warn=True
)

STALE_YEAR_POLICIES = frozenset({"warn", "override", "shift"})


def parse_stale_year_policy(value: object, *, warn: bool = False) -> str:
    """Return warn, override, or shift. Unknown or empty values become warn."""
    raw = str(value or "").strip().lower()
    if not raw:
        return "warn"
    if raw in STALE_YEAR_POLICIES:
        return raw
    if warn:
        print(
            "Warning: STALE_YEAR_POLICY must be warn, override, or shift; using warn. "
            "The value was not logged.",
            file=sys.stderr,
        )
    return "warn"


# Canvas dues in a year before TERM_YEAR. Default warn leaves the raw date
# as-is (current behaviour). override adds a due_overrides.json pin hint.
# shift coerces the year the same way syllabus dates are, unless a pin exists.
STALE_YEAR_POLICY = parse_stale_year_policy(os.getenv("STALE_YEAR_POLICY"), warn=True)

# Local timezone for Calendar timed events / Discord windows
LOCAL_TIMEZONE = (os.getenv("LOCAL_TIMEZONE") or "America/Edmonton").strip()


def infer_term_year(today: date | None = None) -> int:
    """
    Infer academic term year from the execution date.

    Fall (Aug–Dec) → current calendar year.
    Winter/Spring (Jan–Jul) → previous calendar year as the Fall-start year
    for syllabi that omit the year on autumn dates; winter months use current year.
    """
    today = today or date.today()
    override = os.getenv("TERM_YEAR")
    if override and str(override).strip().isdigit():
        return int(str(override).strip())
    if override and str(override).strip():
        print(
            "Warning: TERM_YEAR must be a 4-digit year; using the inferred term year. "
            "The value was not logged.",
            file=sys.stderr,
        )
    # Fall (Aug-Dec) is the current calendar year. Jan-Jul belongs to the
    # academic year that started the previous August.
    if today.month <= 7:
        return today.year - 1
    return today.year


TERM_YEAR = infer_term_year()

# Substrings that appear only in .env.example. Matched case-insensitively.
# Never include a real token here.
_PLACEHOLDER_MARKERS = (
    "your_canvas_api_token",
    "your_gemini_api_key",
    "your_webhook_id",
    "your_webhook_token",
    "your_google_sheet_id",
    "your_email@gmail.com",
    "your-university.instructure.com",
    "your-gcp-project",
    "your-service-account@",
    "replace_with_your_key",
    "your-private-key-id",
    "your-client-id",
    "your-oauth-client",
    "changeme",
    "paste_here",
    "todo_replace",
)


@dataclass(frozen=True)
class ConfigIssue:
    """One configuration finding. `message` must not contain secret values."""

    level: str  # "error" or "warn"
    name: str
    message: str


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(text).split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def installed_tzdata_version() -> str | None:
    try:
        return version("tzdata")
    except PackageNotFoundError:
        return None


def tzdata_is_old(installed: str | None = None) -> bool:
    """True when tzdata is missing or older than the Alberta UTC-6 fix."""
    if installed is None:
        installed = installed_tzdata_version()
    if not installed:
        return True
    return _version_tuple(installed) < _version_tuple(MIN_TZDATA_VERSION)


def tzdata_warning_message() -> str | None:
    if not tzdata_is_old():
        return None
    installed = installed_tzdata_version() or "not installed"
    return (
        f"tzdata {installed} is older than {MIN_TZDATA_VERSION}. "
        "America/Edmonton is wrong after 2026-11-01 because Alberta stays on "
        "UTC-6. Run: pip install \"tzdata>=2026.5\""
    )


def warn_if_tzdata_old() -> None:
    message = tzdata_warning_message()
    if message:
        print(f"Warning: {message}", file=sys.stderr)


def is_placeholder(value: object) -> bool:
    """True when a value is still an example placeholder. Empty is not a placeholder."""
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _timezone_ok(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def _credentials_issues(*, required: bool) -> list[ConfigIssue]:
    """Inspect credentials.json without printing secrets, keys, or emails."""
    from .google_auth import (
        AUTH_OAUTH,
        AUTH_SERVICE_ACCOUNT,
        credentials_kind,
        oauth_client_block,
    )

    path = CREDENTIALS_PATH
    if not path.exists():
        message = (
            "credentials.json is missing. In Google Cloud Console enable the "
            "Sheets API and Calendar API, then save either a service-account "
            "JSON key or a Desktop OAuth client secrets file as credentials.json. "
            "Do not commit it."
        )
        if required:
            return [ConfigIssue("error", "credentials.json", message)]
        return [ConfigIssue("warn", "credentials.json", message)]

    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [
            ConfigIssue(
                "error",
                "credentials.json",
                "credentials.json is not valid UTF-8 JSON. Download a new "
                "service-account key or OAuth client secrets file. "
                "The file contents were not logged.",
            )
        ]

    if not isinstance(data, dict):
        return [
            ConfigIssue(
                "error",
                "credentials.json",
                "credentials.json must be a JSON object (a service-account key "
                "or an OAuth client secrets file).",
            )
        ]

    kind = credentials_kind(data)
    if kind == AUTH_SERVICE_ACCOUNT:
        return _service_account_credential_issues(data)
    if kind == AUTH_OAUTH:
        issues = _oauth_client_credential_issues(data, oauth_client_block)
        if required and not any(item.level == "error" for item in issues) and not TOKEN_PATH.is_file():
            issues.append(
                ConfigIssue(
                    "warn",
                    "token.json",
                    "token.json is not present yet. The first Sheets or Calendar "
                    "run will open a browser to sign in. If the OAuth client is "
                    "in testing mode, add your Google account as a test user.",
                )
            )
        return issues
    return [
        ConfigIssue(
            "error",
            "credentials.json",
            "credentials.json must be a service-account key (type "
            "service_account) or an installed-app / web OAuth client secrets file.",
        )
    ]


def _service_account_credential_issues(data: dict) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for field in ("client_email", "private_key", "token_uri"):
        if not str(data.get(field) or "").strip():
            issues.append(
                ConfigIssue(
                    "error",
                    "credentials.json",
                    f"credentials.json is missing '{field}'. Download a new key. "
                    "The field value was not logged.",
                )
            )
    private_key = str(data.get("private_key") or "")
    client_email = str(data.get("client_email") or "")
    if is_placeholder(private_key) or is_placeholder(client_email) or "REPLACE_WITH" in private_key:
        issues.append(
            ConfigIssue(
                "error",
                "credentials.json",
                "credentials.json still contains example placeholder values. "
                "Replace it with the JSON key downloaded from Google Cloud.",
            )
        )
    return issues


def _oauth_client_credential_issues(data: dict, block_fn) -> list[ConfigIssue]:
    block = block_fn(data)
    if not isinstance(block, dict):
        return [
            ConfigIssue(
                "error",
                "credentials.json",
                "OAuth client secrets file is missing the installed or web client block.",
            )
        ]
    issues: list[ConfigIssue] = []
    for field in ("client_id", "client_secret", "token_uri"):
        if not str(block.get(field) or "").strip():
            issues.append(
                ConfigIssue(
                    "error",
                    "credentials.json",
                    f"credentials.json is missing '{field}'. Download a new "
                    "Desktop OAuth client secrets file. The field value was not logged.",
                )
            )
    client_id = str(block.get("client_id") or "")
    client_secret = str(block.get("client_secret") or "")
    if is_placeholder(client_id) or is_placeholder(client_secret):
        issues.append(
            ConfigIssue(
                "error",
                "credentials.json",
                "credentials.json still contains example placeholder values. "
                "Replace it with the OAuth client secrets file from Google Cloud.",
            )
        )
    return issues


def _url_issue(name: str, value: str, *, kind: str) -> ConfigIssue | None:
    if is_placeholder(value):
        return ConfigIssue(
            "error",
            name,
            f"{name} is still the example placeholder. Put the real value in .env "
            "or local.env. The value was not logged.",
        )
    lowered = value.lower()
    if kind == "canvas" and not (lowered.startswith("https://") or lowered.startswith("http://")):
        return ConfigIssue(
            "error",
            name,
            f"{name} must be an http(s) URL such as https://school.instructure.com. "
            "The value was not logged.",
        )
    if kind == "discord" and "/api/webhooks/" not in lowered:
        return ConfigIssue(
            "error",
            name,
            f"{name} should look like https://discord.com/api/webhooks/... "
            "The value was not logged.",
        )
    if kind == "calendar" and (
        "calendar.google.com" in lowered or lowered.endswith(".ics") or lowered.startswith("http")
    ):
        return ConfigIssue(
            "error",
            name,
            f"{name} looks like an iCal or share URL. Use your Gmail address or the "
            "calendar ID from Calendar Settings → Integrate calendar.",
        )
    return None



def _sheet_layout_issues() -> list[ConfigIssue]:
    """Validate optional sheet-layout overrides. Defaults need no check."""
    issues: list[ConfigIssue] = []
    raw_row = os.getenv("SHEET_DATA_START_ROW")
    if raw_row is not None and str(raw_row).strip():
        try:
            number = int(str(raw_row).strip())
        except ValueError:
            number = 0
        if number < 1:
            issues.append(
                ConfigIssue(
                    "error",
                    "SHEET_DATA_START_ROW",
                    "SHEET_DATA_START_ROW must be a positive integer (default 11). "
                    "The value was not logged.",
                )
            )
    return issues


def collect_config_issues(
    *,
    require_discord: bool = False,
    require_google: bool = False,
    require_calendar: bool = False,
    require_gemini: bool = False,
    require_canvas: bool = True,
) -> list[ConfigIssue]:
    """
    Return configuration problems.

    Messages name the variable only. They never include token, key, or webhook values.
    """
    issues: list[ConfigIssue] = []

    def require(name: str, value: str, *, kind: str = "text") -> None:
        if not value:
            issues.append(
                ConfigIssue(
                    "error",
                    name,
                    f"{name} is missing. Copy examples/.env.example to .env (or local.env) and set it.",
                )
            )
            return
        if is_placeholder(value):
            issues.append(
                ConfigIssue(
                    "error",
                    name,
                    f"{name} is still the example placeholder. Replace it in .env or local.env. "
                    "The value was not logged.",
                )
            )
            return
        extra = _url_issue(name, value, kind=kind) if kind != "text" else None
        if extra:
            issues.append(extra)

    if require_canvas:
        require("CANVAS_URL", CANVAS_URL, kind="canvas")
        require("CANVAS_TOKEN", CANVAS_TOKEN)
    else:
        if not CANVAS_URL or not CANVAS_TOKEN or is_placeholder(CANVAS_URL) or is_placeholder(CANVAS_TOKEN):
            issues.append(
                ConfigIssue(
                    "warn",
                    "CANVAS_URL",
                    "CANVAS_URL / CANVAS_TOKEN are missing or still placeholders. Fine for --syllabus-only and --dry-run; required for a normal run.",
                )
            )

    if require_google:
        require("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID)
    elif not GOOGLE_SHEET_ID:
        issues.append(
            ConfigIssue(
                "warn",
                "GOOGLE_SHEET_ID",
                "GOOGLE_SHEET_ID is not set. Required for --google / --all.",
            )
        )
    elif is_placeholder(GOOGLE_SHEET_ID):
        issues.append(
            ConfigIssue(
                "warn",
                "GOOGLE_SHEET_ID",
                "GOOGLE_SHEET_ID is still the example placeholder. Required for --google / --all.",
            )
        )

    if require_calendar:
        require("GOOGLE_CALENDAR_ID", GOOGLE_CALENDAR_ID, kind="calendar")
    elif not GOOGLE_CALENDAR_ID:
        issues.append(
            ConfigIssue(
                "warn",
                "GOOGLE_CALENDAR_ID",
                "GOOGLE_CALENDAR_ID is not set. Required for --calendar / --all. "
                "Use an email or calendar ID, not an iCal URL.",
            )
        )
    else:
        extra = _url_issue("GOOGLE_CALENDAR_ID", GOOGLE_CALENDAR_ID, kind="calendar")
        if extra:
            extra = ConfigIssue("warn" if not require_calendar else "error", extra.name, extra.message)
            issues.append(extra)

    if require_discord:
        require("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL, kind="discord")
    elif not DISCORD_WEBHOOK_URL:
        issues.append(
            ConfigIssue(
                "warn",
                "DISCORD_WEBHOOK_URL",
                "DISCORD_WEBHOOK_URL is not set. Required for --weekly / --all.",
            )
        )
    elif is_placeholder(DISCORD_WEBHOOK_URL) or "/api/webhooks/" not in DISCORD_WEBHOOK_URL.lower():
        issues.append(
            ConfigIssue(
                "warn",
                "DISCORD_WEBHOOK_URL",
                "DISCORD_WEBHOOK_URL is missing or still a placeholder. "
                "Required for --weekly / --all. The value was not logged.",
            )
        )

    if require_gemini and not GEMINI_API_KEY:
        issues.append(
            ConfigIssue(
                "error",
                "GEMINI_API_KEY",
                "GEMINI_API_KEY is missing. Syllabus parsing can run without it.",
            )
        )
    elif require_gemini and is_placeholder(GEMINI_API_KEY):
        issues.append(
            ConfigIssue(
                "error",
                "GEMINI_API_KEY",
                "GEMINI_API_KEY is still the example placeholder. The value was not logged.",
            )
        )
    elif not GEMINI_API_KEY or is_placeholder(GEMINI_API_KEY):
        issues.append(
            ConfigIssue(
                "warn",
                "GEMINI_API_KEY",
                "GEMINI_API_KEY is not set. PDF parsing uses the built-in syllabus parser only.",
            )
        )

    if not _timezone_ok(LOCAL_TIMEZONE):
        issues.append(
            ConfigIssue(
                "error",
                "LOCAL_TIMEZONE",
                "LOCAL_TIMEZONE is not a valid IANA timezone (example: America/Edmonton). "
                "The value was not logged.",
            )
        )

    issues.extend(_sheet_layout_issues())
    issues.extend(_credentials_issues(required=require_google or require_calendar))

    if not SCHEDULE_PATH.exists():
        issues.append(
            ConfigIssue(
                "warn",
                "schedule.json",
                "schedule.json is missing. Copy examples/schedule.example.json to schedule.json "
                "and fill in your lecture/lab sections. Without it, section filtering is off.",
            )
        )

    excel_parent = Path(EXCEL_FILE_PATH).expanduser()
    if not excel_parent.is_absolute():
        excel_parent = BASE_DIR / excel_parent
    parent = excel_parent.parent
    if parent.exists() and not os.access(parent, os.W_OK):
        issues.append(
            ConfigIssue(
                "warn",
                "EXCEL_FILE_PATH",
                "The Excel output folder is not writable. Close the workbook if it is "
                "open, and wait if OneDrive is locking the file.",
            )
        )

    tz_message = tzdata_warning_message()
    if tz_message:
        issues.append(ConfigIssue("warn", "tzdata", tz_message))

    return issues


def format_config_report(issues: list[ConfigIssue]) -> str:
    """Plain-text report. Safe to print: issues never carry secret values."""
    if not issues:
        return "Configuration check passed. No missing required settings."
    lines = ["Configuration check:"]
    for issue in issues:
        label = "ERROR" if issue.level == "error" else "WARN"
        lines.append(f"  [{label}] {issue.message}")
    errors = [i for i in issues if i.level == "error"]
    if errors:
        lines.append(
            f"{len(errors)} required item(s) need attention. "
            "Fix .env / local.env or credentials.json, then re-run: python main.py --check"
        )
    else:
        lines.append("No required settings are missing. Warnings are optional integrations.")
    return "\n".join(lines)


def validate_env(
    require_discord: bool = False,
    require_google: bool = False,
    require_calendar: bool = False,
    require_gemini: bool = False,
    require_canvas: bool = True,
    *,
    strict: bool = True,
) -> list[ConfigIssue]:
    """
    Check environment and credentials.

    When strict is True (a live run), exit if any error-level issue is present.
    Dry-run and --check pass strict=False and decide for themselves.
    """
    issues = collect_config_issues(
        require_discord=require_discord,
        require_google=require_google,
        require_calendar=require_calendar,
        require_gemini=require_gemini,
        require_canvas=require_canvas,
    )
    errors = [issue for issue in issues if issue.level == "error"]
    if strict and errors:
        print(format_config_report(issues), file=sys.stderr)
        sys.exit(1)
    return issues
