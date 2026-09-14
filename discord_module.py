"""Send a weekly assignment digest to Discord via webhook."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import requests

from config import DISCORD_WEBHOOK_URL, LOCAL_TIMEZONE
from date_utils import due_window_bounds, has_explicit_time, local_tz, parse_due_datetime
from dedupe_module import is_droppable_placeholder, is_phantom_task
from retry_utils import with_retries
from vocab import Status, normalize_priority, normalize_status

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x5865F2  # Discord blurple
LOOKAHEAD_DAYS = 7

# Finished work — skip these in the weekly digest notification
_DIGEST_SKIP_STATUSES = {
    Status.COMPLETE,
    Status.GRADED,
    Status.SUBMITTED,
    Status.CANCELLED,
}

# Priority → Discord embed accent (field-level not supported; used for logging)
PRIORITY_HINT = {
    "CRITICAL": 0xE74C3C,
    "HIGH": 0xE67E22,
    "MEDIUM": 0xF1C40F,
    "LOW": 0x3498DB,
}


def _is_finished_status(status: object) -> bool:
    """True when Status is Complete / Done / Graded / Submitted / Cancelled."""
    return normalize_status(status) in _DIGEST_SKIP_STATUSES


def _filter_upcoming(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return rows with due dates within today..+LOOKAHEAD_DAYS (local TZ).

    Date-only items due "today" remain included through 23:59:59 local time
    so midnight edge cases don't drop them after 00:00.
    Skips assignments already completed / submitted / graded / cancelled.
    """
    if df.empty:
        return df

    start, end = due_window_bounds(lookahead_days=LOOKAHEAD_DAYS)

    df = df.copy()
    parsed_dues: list[datetime | None] = []
    for value in df["Due Date"]:
        parsed_dues.append(parse_due_datetime(value))
    df["_parsed_date"] = parsed_dues

    def _in_window(dt: datetime | None) -> bool:
        if dt is None:
            return False
        # Date-only stored as midnight still counts for that whole local day
        return start <= dt <= end

    mask = df["_parsed_date"].map(_in_window)
    if "Status" in df.columns:
        mask &= ~df["Status"].map(_is_finished_status)
    if "Is Draft" in df.columns:
        mask &= ~df["Is Draft"].map(
            lambda v: str(v).strip().lower() in {"1", "true", "yes", "draft"}
        )

    # Safety net: drop bare "Labs"/"Assignments" even if Is Draft was lost on Sheets
    title_col = "Assessment title" if "Assessment title" in df.columns else "Task"
    if title_col in df.columns:
        mask &= ~df.apply(
            lambda row: is_droppable_placeholder(row.get(title_col), row.get("Due Date"))
            or is_phantom_task(row.get(title_col), row.get("Due Date")),
            axis=1,
        )

    upcoming = df.loc[mask].drop(columns="_parsed_date")
    return upcoming.sort_values("Due Date")


def _format_due_date(due_date: str) -> str:
    """Format a due date string for display in the embed."""
    parsed = parse_due_datetime(due_date)
    if parsed is None:
        return str(due_date)
    # Date-only values show as calendar day (avoid misleading "12:00 AM")
    if not has_explicit_time(due_date):
        return parsed.strftime("%a, %b %d")
    return parsed.strftime("%a, %b %d · %I:%M %p")


def _build_embed(upcoming: pd.DataFrame) -> dict:
    """Build a Discord embed payload from upcoming assignments."""
    tz = local_tz()
    now = datetime.now(tz)
    start, end = due_window_bounds(now=now, lookahead_days=LOOKAHEAD_DAYS)
    today = start.strftime("%B %d, %Y")
    end_label = end.strftime("%B %d, %Y")

    if upcoming.empty:
        return {
            "title": "Sylla Sync — Weekly Digest",
            "description": (
                f"**No assignments due between {today} and {end_label}.**\n"
                f"Timezone: {LOCAL_TIMEZONE}"
            ),
            "color": EMBED_COLOR,
            "footer": {"text": "Sylla Sync · Your assignment tracker"},
        }

    grouped: dict[str, list[str]] = {}
    title_col = "Assessment title" if "Assessment title" in upcoming.columns else "Task"

    for _, row in upcoming.iterrows():
        course = row["Course"]
        pri = normalize_priority(row.get("Priority", "")).value
        hint = f" [{pri}]" if pri in {"HIGH", "CRITICAL"} else ""
        task_line = (
            f"• **{row[title_col]}**{hint} — {_format_due_date(row['Due Date'])}"
        )
        grouped.setdefault(course, []).append(task_line)

    fields = []
    for course, tasks in grouped.items():
        fields.append(
            {
                "name": str(course),
                "value": "\n".join(tasks),
                "inline": False,
            }
        )

    # Tint embed by highest priority present
    max_pri = "LOW"
    for _, row in upcoming.iterrows():
        p = normalize_priority(row.get("Priority", "")).value
        order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
        if order.index(p) > order.index(max_pri):
            max_pri = p

    return {
        "title": "Sylla Sync — Weekly Digest",
        "description": (
            f"**{len(upcoming)} assignment(s) due** between "
            f"**{today}** and **{end_label}** ({LOCAL_TIMEZONE})."
        ),
        "color": PRIORITY_HINT.get(max_pri, EMBED_COLOR),
        "fields": fields,
        "footer": {"text": "Sylla Sync · Your assignment tracker"},
    }


@with_retries(label="discord.webhook")
def _post_webhook(url: str, payload: dict) -> requests.Response:
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response


def send_weekly_digest(df: pd.DataFrame, *, dry_run: bool = False) -> None:
    """
    Filter tasks due within 7 days and POST a rich embed to Discord.

    Raises:
        ValueError: If DISCORD_WEBHOOK_URL is not configured.
        requests.HTTPError: If the webhook request fails.
    """
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("DISCORD_WEBHOOK_URL must be set in .env")

    upcoming = _filter_upcoming(df)
    embed = _build_embed(upcoming)
    payload = {"embeds": [embed]}

    logger.info(
        "Weekly digest (%d upcoming task(s)) dry_run=%s",
        len(upcoming),
        dry_run,
    )

    if dry_run:
        print(
            f"      [dry-run] Would send Discord digest "
            f"({len(upcoming)} upcoming task(s))."
        )
        for _, row in upcoming.head(15).iterrows():
            title_col = (
                "Assessment title" if "Assessment title" in upcoming.columns else "Task"
            )
            print(f"        • {row['Course']}: {row[title_col]} ({row['Due Date']})")
        return

    _post_webhook(DISCORD_WEBHOOK_URL, payload)
    logger.info("Discord digest sent successfully.")
