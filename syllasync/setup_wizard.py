"""Interactive first-run setup: write .env from prompts.

Stdlib only. Never prints secret values. Does not read or write
credentials.json, except to show a service-account client_email so the
user knows who to share the Sheet and Calendar with.
"""

from __future__ import annotations

import getpass
import json
import re
from collections.abc import Callable
from pathlib import Path

SECRET_KEYS = frozenset(
    {"CANVAS_TOKEN", "GEMINI_API_KEY", "DISCORD_WEBHOOK_URL"}
)
OPTIONAL_KEYS = frozenset(
    {"DISCORD_WEBHOOK_URL", "GEMINI_API_KEY", "TERM_YEAR", "GEMINI_MODEL"}
)
_EXAMPLE_MARKERS = (
    "your_",
    "your-",
    "your-university",
    "your_google_sheet",
    "your_email@",
    "your_gemini",
    "your_webhook",
    "your_canvas",
)
# Keys the wizard prompts for, in order.
FIELD_NAMES = (
    "CANVAS_URL",
    "CANVAS_TOKEN",
    "GOOGLE_SHEET_ID",
    "GOOGLE_CALENDAR_ID",
    "DISCORD_WEBHOOK_URL",
    "GEMINI_API_KEY",
    "LOCAL_TIMEZONE",
    "TERM_YEAR",
    "SYNC_GRADES",
)
_ASSIGN_RE = re.compile(r"^(\s*#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def is_secret(name: str) -> bool:
    return name in SECRET_KEYS


def is_example_value(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _EXAMPLE_MARKERS)


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=value lines. Comments and blank lines are skipped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        values[key] = _unquote(raw.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def format_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch in value for ch in ' \t#\'"\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def upsert_env(template: str, values: dict[str, str]) -> str:
    """Update KEY=value lines in *template*; append keys that were not present."""
    seen: set[str] = set()
    lines: list[str] = []
    for line in template.splitlines():
        match = _ASSIGN_RE.match(line)
        if not match:
            lines.append(line)
            continue
        key = match.group(2)
        if key not in values:
            lines.append(line)
            continue
        seen.add(key)
        lines.append(_assignment_line(key, values[key]))
    for key, value in values.items():
        if key in seen:
            continue
        if value == "" and key in OPTIONAL_KEYS:
            continue
        lines.append(_assignment_line(key, value))
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if not text:
        text = "\n".join(_assignment_line(k, v) for k, v in values.items() if v or k not in OPTIONAL_KEYS)
        if text:
            text += "\n"
    return text


def _assignment_line(key: str, value: str) -> str:
    if value == "" and key in OPTIONAL_KEYS:
        return f"# {key}="
    return f"{key}={format_env_value(value)}"


def read_client_email(credentials_path: Path) -> str | None:
    """Return the service-account client_email, or None. Never returns the key."""
    try:
        data = json.loads(credentials_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    email = str(data.get("client_email") or "").strip()
    return email or None


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8-sig"))


def _ask(prompt: str, input_fn: Callable[[str], str]) -> str:
    return input_fn(prompt).strip()


def _ask_yes_no(
    prompt: str,
    input_fn: Callable[[str], str],
    *,
    default: bool = True,
) -> bool:
    raw = _ask(prompt, input_fn).lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _field_prompt(name: str, *, optional: bool) -> str:
    labels = {
        "CANVAS_URL": "Canvas base URL (e.g. https://school.instructure.com)",
        "CANVAS_TOKEN": "Canvas API token",
        "GOOGLE_SHEET_ID": "Google Sheet ID",
        "GOOGLE_CALENDAR_ID": "Google Calendar ID (email or calendar ID, not an iCal URL)",
        "DISCORD_WEBHOOK_URL": "Discord webhook URL",
        "GEMINI_API_KEY": "Gemini API key",
        "LOCAL_TIMEZONE": "Local timezone (IANA, e.g. America/Edmonton)",
        "TERM_YEAR": "Term year (Fall start year)",
        "SYNC_GRADES": "Pull Canvas grades into the sheet?",
    }
    label = labels.get(name, name)
    if name == "SYNC_GRADES":
        return f"{label} [Y/n]: "
    if name == "TERM_YEAR":
        return f"{label}, blank to infer: "
    if name == "LOCAL_TIMEZONE":
        return f"{label} [America/Edmonton]: "
    if optional:
        return f"{label} (optional, blank to skip): "
    return f"{label}: "


def collect_values(
    existing: dict[str, str],
    *,
    input_fn: Callable[[str], str],
    secret_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> dict[str, str]:
    """Prompt for each field. Secret values are never written to *print_fn*."""
    result = dict(existing)
    for name in FIELD_NAMES:
        optional = name in OPTIONAL_KEYS
        current = str(existing.get(name, "") or "").strip()
        if current:
            if is_secret(name):
                print_fn(f"{name} is already set (value hidden).")
            else:
                print_fn(f"{name} is currently {current}.")
            if _ask_yes_no(f"Keep {name}? [Y/n]: ", input_fn, default=True):
                result[name] = current
                continue
        if name == "SYNC_GRADES":
            yes = _ask_yes_no(_field_prompt(name, optional=False), input_fn, default=True)
            result[name] = "1" if yes else "0"
            continue
        prompt = _field_prompt(name, optional=optional)
        if is_secret(name):
            raw = secret_fn(prompt).strip()
        else:
            raw = _ask(prompt, input_fn)
        if not raw:
            if name == "LOCAL_TIMEZONE" and not current:
                result[name] = "America/Edmonton"
            elif optional:
                result[name] = ""
            elif current:
                result[name] = current
            else:
                while not raw:
                    print_fn(f"{name} is required.")
                    if is_secret(name):
                        raw = secret_fn(prompt).strip()
                    else:
                        raw = _ask(prompt, input_fn)
                result[name] = raw
        else:
            result[name] = raw
    return result


def _copy_schedule_if_missing(repo: Path, print_fn: Callable[[str], None]) -> None:
    dest = repo / "schedule.json"
    src = repo / "examples" / "schedule.example.json"
    if dest.exists() or not src.is_file():
        return
    dest.write_bytes(src.read_bytes())
    print_fn("Copied examples/schedule.example.json to schedule.json.")


def run_setup(
    repo_dir: str | Path,
    *,
    input_fn: Callable[[str], str] | None = None,
    secret_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> int:
    """Walk through .env prompts and write the file. Returns an exit code."""
    repo = Path(repo_dir)
    env_path = repo / ".env"
    example_path = repo / "examples" / ".env.example"
    creds_path = repo / "credentials.json"
    ask = input_fn or input
    secret = secret_fn or getpass.getpass
    out = print_fn or (lambda msg: print(msg))

    out("SyllaSync setup. Answers go into .env only.")
    out("credentials.json is not changed. Secret values are not printed.")

    if env_path.is_file():
        template = env_path.read_text(encoding="utf-8-sig")
        existing = parse_env(template)
        out(".env already exists. You can keep or replace each value.")
    else:
        template = (
            example_path.read_text(encoding="utf-8-sig") if example_path.is_file() else ""
        )
        existing = parse_env(template)
        # Example placeholders should not be treated as real saved values.
        existing = {
            key: value
            for key, value in existing.items()
            if value and not is_example_value(value)
        }

    values = collect_values(
        existing, input_fn=ask, secret_fn=secret, print_fn=out
    )
    text = upsert_env(template, values)
    env_path.write_text(text, encoding="utf-8", newline="\n")
    out("Wrote .env. Secret values were not printed.")

    _copy_schedule_if_missing(repo, out)

    if creds_path.is_file():
        email = read_client_email(creds_path)
        if email:
            out(
                "Share the Sheet (Editor) and Calendar (Make changes to events) "
                f"with {email}."
            )
        else:
            out(
                "credentials.json is present, but no client_email was found. "
                "Share the Sheet and Calendar with the service account email."
            )
    else:
        out(
            "Put a Google service-account key in credentials.json "
            "(see examples/credentials.json.example). Share the Sheet (Editor) "
            "and Calendar (Make changes to events) with its client_email."
        )

    out("Next: python main.py --check --all")
    out("Then preview with: python main.py --all --dry-run")
    return 0


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    raise SystemExit(run_setup(repo))


if __name__ == "__main__":
    main()
