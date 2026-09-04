"""
Push Sylla Sync assignments to Google Calendar as all-day events.

Requires:
  - GOOGLE_CALENDAR_ID in .env / local.env (email or calendar ID, not an iCal URL)
  - credentials.json service-account key in the project root
  - Calendar shared with the service account email
    (permission: "Make changes to events")
  - Google Calendar API enabled in the same GCP project
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import BASE_DIR, GOOGLE_CALENDAR_ID
from date_utils import normalize_calendar_date

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
]

# Google Calendar colorIds by priority
PRIORITY_COLOR = {
    "high": "11",    # Red
    "medium": "5",   # Yellow
    "med": "5",
    "low": "9",      # Blue
}


class GoogleCalendarPermissionError(PermissionError):
    """Raised when the service account cannot access the target calendar."""


def _authenticate():
    """Build an authenticated Google Calendar API service."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Download a service-account key from Google Cloud Console."
        )
    if not GOOGLE_CALENDAR_ID:
        raise ValueError(
            "GOOGLE_CALENDAR_ID must be set in .env "
            "(your email, or a dedicated calendar ID — not an iCal URL)."
        )
    if "calendar.google.com" in GOOGLE_CALENDAR_ID or GOOGLE_CALENDAR_ID.endswith(".ics"):
        raise ValueError(
            "GOOGLE_CALENDAR_ID looks like an iCal/share URL. "
            "Use your email (e.g. you@gmail.com) or the Calendar ID from "
            "Settings → Integrate calendar."
        )

    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _priority_color(priority: str) -> str | None:
    """Map a Priority cell value to a Google Calendar colorId."""
    return PRIORITY_COLOR.get(priority.strip().lower())


def _build_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Sylla Sync assignment row into a Calendar event body."""
    title_col = "Assessment title" if "Assessment title" in record else "Task"
    title = str(record.get(title_col, "") or "").strip()
    course = str(record.get("Course", "") or "").strip()
    due_raw = record.get("Due Date", "")

    iso_date = normalize_calendar_date(due_raw)
    if not iso_date or not title:
        return None

    # e.g. "MATH 201 - Midterm Exam"
    summary = f"{course} - {title}" if course else title

    # All-day events: end.date is exclusive (must be the day AFTER start).
    end_date = (
        datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    priority = str(record.get("Priority", "") or "").strip() or "—"
    status = str(record.get("Status", "") or "").strip() or "—"
    est_time = str(
        record.get("Estimated time dedicated to task", "") or ""
    ).strip() or "—"

    event: dict[str, Any] = {
        "summary": summary,
        "description": (
            f"Source: Sylla Sync\n"
            f"Priority: {priority}\n"
            f"Status: {status}\n"
            f"Estimated Time: {est_time}"
        ),
        "start": {"date": iso_date},
        "end": {"date": end_date},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 24 * 60}],
        },
    }

    color = _priority_color(priority)
    if color:
        event["colorId"] = color

    return event


def _get_existing_by_summary(service, calendar_id: str) -> dict[str, str]:
    """
    Fetch existing events and map summary → event_id.

    Matching on summary alone so a due-date change in Canvas updates
    the same calendar event instead of creating a duplicate.
    """
    existing: dict[str, str] = {}
    page_token = None
    now = datetime.utcnow()
    time_min = (now - timedelta(days=90)).isoformat() + "Z"
    time_max = (now + timedelta(days=730)).isoformat() + "Z"

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=500,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            )
            .execute()
        )

        for item in result.get("items", []):
            summary = (item.get("summary") or "").strip()
            if summary and summary not in existing:
                existing[summary] = item["id"]

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return existing


def push_to_google_calendar(df: pd.DataFrame) -> dict[str, int]:
    """
    Create or update Google Calendar events from a Sylla Sync DataFrame.

    Deduplicates by event Summary: existing → update, missing → insert.

    Returns:
        Counts dict with keys: added, updated, skipped, failed.
    """
    print(f"\n--- Google Calendar sync → {GOOGLE_CALENDAR_ID} ---")

    try:
        service = _authenticate()
        calendar_id = GOOGLE_CALENDAR_ID
        existing = _get_existing_by_summary(service, calendar_id)
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status in (403, 404):
            raise GoogleCalendarPermissionError(
                "Cannot access Google Calendar. Share the calendar with the "
                "service account client_email from credentials.json "
                "(permission: 'Make changes to events'), set GOOGLE_CALENDAR_ID "
                "to your email or calendar ID (not an iCal URL), and enable "
                "the Google Calendar API in GCP."
            ) from exc
        raise

    print(f"Found {len(existing)} existing event(s) for dedup by summary.")

    counts = {"added": 0, "updated": 0, "skipped": 0, "failed": 0}

    for record in df.to_dict(orient="records"):
        event = _build_event(record)
        if not event:
            counts["skipped"] += 1
            continue

        summary = event["summary"]
        start_date = event["start"]["date"]

        try:
            if summary in existing:
                service.events().update(
                    calendarId=calendar_id,
                    eventId=existing[summary],
                    body=event,
                ).execute()
                counts["updated"] += 1
                print(f"  ~ updated  {summary} ({start_date})")
            else:
                created = (
                    service.events()
                    .insert(calendarId=calendar_id, body=event)
                    .execute()
                )
                existing[summary] = created["id"]
                counts["added"] += 1
                print(f"  + added    {summary} ({start_date})")
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (403, 404):
                raise GoogleCalendarPermissionError(
                    "Calendar permission denied while writing events. "
                    "Share the calendar with your service account "
                    "(Make changes to events) and verify GOOGLE_CALENDAR_ID."
                ) from exc
            counts["failed"] += 1
            print(f"  ! failed   {summary}: {exc}")
            logger.error("Failed to sync event '%s': %s", summary, exc)

    print(
        f"\nCalendar done — "
        f"added: {counts['added']}, "
        f"updated: {counts['updated']}, "
        f"skipped (no date/title): {counts['skipped']}, "
        f"failed: {counts['failed']}\n"
    )
    logger.info(
        "Calendar sync: +%d ~%d skip=%d fail=%d",
        counts["added"],
        counts["updated"],
        counts["skipped"],
        counts["failed"],
    )
    return counts
