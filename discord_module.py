"""Send a weekly assignment digest to Discord via webhook."""

import logging
from datetime import datetime, timedelta

import pandas as pd
import requests

from config import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x5865F2  # Discord blurple
LOOKAHEAD_DAYS = 7


def _filter_upcoming(df: pd.DataFrame) -> pd.DataFrame:
    """Return rows with due dates within the next 7 days."""
    if df.empty:
        return df

    now = datetime.now()
    cutoff = now + timedelta(days=LOOKAHEAD_DAYS)

    df = df.copy()
    df["_parsed_date"] = pd.to_datetime(df["Due Date"], errors="coerce")

    mask = (df["_parsed_date"] >= now) & (df["_parsed_date"] <= cutoff)
    upcoming = df.loc[mask].drop(columns="_parsed_date")
    return upcoming.sort_values("Due Date")


def _format_due_date(due_date: str) -> str:
    """Format a due date string for display in the embed."""
    parsed = pd.to_datetime(due_date, errors="coerce")
    if pd.isna(parsed):
        return due_date
    return parsed.strftime("%a, %b %d · %I:%M %p")


def _build_embed(upcoming: pd.DataFrame) -> dict:
    """Build a Discord embed payload from upcoming assignments."""
    today = datetime.now().strftime("%B %d, %Y")
    end = (datetime.now() + timedelta(days=LOOKAHEAD_DAYS)).strftime("%B %d, %Y")

    if upcoming.empty:
        return {
            "title": "📚 Sylla Sync — Weekly Digest",
            "description": (
                f"**No assignments due between {today} and {end}.**\n"
                "Enjoy the breather! 🎉"
            ),
            "color": EMBED_COLOR,
            "footer": {"text": "Sylla Sync · Your assignment tracker"},
        }

    # Group tasks by course for cleaner formatting.
    grouped: dict[str, list[str]] = {}
    title_col = "Assessment title" if "Assessment title" in upcoming.columns else "Task"

    for _, row in upcoming.iterrows():
        course = row["Course"]
        task_line = f"• **{row[title_col]}** — {_format_due_date(row['Due Date'])}"
        grouped.setdefault(course, []).append(task_line)

    fields = []
    for course, tasks in grouped.items():
        fields.append(
            {
                "name": f"📖 {course}",
                "value": "\n".join(tasks),
                "inline": False,
            }
        )

    return {
        "title": "📚 Sylla Sync — Weekly Digest",
        "description": (
            f"**{len(upcoming)} assignment(s) due** between **{today}** and **{end}**."
        ),
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": "Sylla Sync · Your assignment tracker"},
    }


def send_weekly_digest(df: pd.DataFrame) -> None:
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
        "Sending weekly digest (%d upcoming task(s)) to Discord.",
        len(upcoming),
    )

    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        logger.error(
            "Discord webhook failed (%s): %s",
            response.status_code,
            response.text,
        )
        raise exc

    logger.info("Discord digest sent successfully.")
