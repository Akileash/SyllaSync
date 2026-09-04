"""
Push Sylla Sync assignments to Google Calendar as all-day events.

Idempotent upserts use extendedProperties.private.task_id so re-runs
update or skip instead of creating duplicates.

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
from dedupe_module import TASK_ID_COL, ensure_task_id

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
]

PRIORITY_COLOR = {
    "high": "11",
    "medium": "5",
    "med": "5",
    "low": "9",
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

    summary = f"{course} - {title}" if course else title
    end_date = (
        datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    priority = str(record.get("Priority", "") or "").strip() or "—"
    status = str(record.get("Status", "") or "").strip() or "—"
    est_time = str(
        record.get("Estimated time dedicated to task", "") or ""
    ).strip() or "—"

    task_id = str(record.get(TASK_ID_COL) or "").strip() or ensure_task_id(
        {
            **record,
            "Task": title,
            "Course": course,
            "Due Date": due_raw,
        }
    )

    event: dict[str, Any] = {
        "summary": summary,
        "description": (
            f"Source: Sylla Sync\n"
            f"Task ID: {task_id}\n"
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
        "extendedProperties": {
            "private": {
                "task_id": task_id,
                "syllasync": "1",
            }
        },
    }

    color = _priority_color(priority)
    if color:
        event["colorId"] = color

    return event


def _event_start_date(item: dict[str, Any]) -> str:
    start = item.get("start") or {}
    if start.get("date"):
        return start["date"]
    if start.get("dateTime"):
        return str(start["dateTime"])[:10]
    return ""


def _get_existing_events(service, calendar_id: str) -> dict[str, dict[str, Any]]:
    """
    Index existing events by task_id (preferred) and by summary||date fallback.

    Returns:
        {
          "by_task_id": {task_id: {id, summary, start_date, raw}},
          "by_summary_date": {f"{summary}||{date}": {...}},
        }
    """
    by_task_id: dict[str, dict[str, Any]] = {}
    by_summary_date: dict[str, dict[str, Any]] = {}
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
            start_date = _event_start_date(item)
            private = ((item.get("extendedProperties") or {}).get("private")) or {}
            task_id = str(private.get("task_id") or "").strip()

            meta = {
                "id": item["id"],
                "summary": summary,
                "start_date": start_date,
                "task_id": task_id,
                "raw": item,
            }
            if task_id and task_id not in by_task_id:
                by_task_id[task_id] = meta
            if summary and start_date:
                by_summary_date.setdefault(f"{summary}||{start_date}", meta)
            elif summary:
                by_summary_date.setdefault(f"{summary}||", meta)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return {"by_task_id": by_task_id, "by_summary_date": by_summary_date}


def _events_equivalent(existing_meta: dict[str, Any], event: dict[str, Any]) -> bool:
    """True if summary + start date already match (no patch needed)."""
    return (
        existing_meta.get("summary") == event.get("summary")
        and existing_meta.get("start_date") == (event.get("start") or {}).get("date")
    )


def push_to_google_calendar(df: pd.DataFrame) -> dict[str, int]:
    """
    Create / update / skip Google Calendar events using Task_ID upserts.

    Returns:
        Counts: created, updated, unchanged, skipped, failed
        (also aliases added→created for older callers).
    """
    print(f"\n--- Google Calendar sync → {GOOGLE_CALENDAR_ID} ---")

    try:
        service = _authenticate()
        calendar_id = GOOGLE_CALENDAR_ID
        existing = _get_existing_events(service, calendar_id)
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

    by_task_id = existing["by_task_id"]
    by_summary_date = existing["by_summary_date"]
    print(
        f"Found {len(by_task_id)} event(s) with task_id, "
        f"{len(by_summary_date)} summary/date index entries."
    )

    counts = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        # aliases
        "added": 0,
    }

    seen_task_ids: set[str] = set()

    for record in df.to_dict(orient="records"):
        event = _build_event(record)
        if not event:
            counts["skipped"] += 1
            continue

        task_id = event["extendedProperties"]["private"]["task_id"]
        if task_id in seen_task_ids:
            counts["skipped"] += 1
            continue
        seen_task_ids.add(task_id)

        summary = event["summary"]
        start_date = event["start"]["date"]
        match = by_task_id.get(task_id)
        if not match:
            match = by_summary_date.get(f"{summary}||{start_date}") or by_summary_date.get(
                f"{summary}||"
            )

        try:
            if match:
                if _events_equivalent(match, event):
                    # Still patch extendedProperties if legacy event lacked task_id
                    if not match.get("task_id"):
                        service.events().patch(
                            calendarId=calendar_id,
                            eventId=match["id"],
                            body={
                                "extendedProperties": event["extendedProperties"],
                                "description": event["description"],
                            },
                        ).execute()
                    counts["unchanged"] += 1
                    print(f"  = unchanged {summary} ({start_date})")
                else:
                    service.events().update(
                        calendarId=calendar_id,
                        eventId=match["id"],
                        body=event,
                    ).execute()
                    counts["updated"] += 1
                    print(f"  ~ updated  {summary} ({start_date})")
                by_task_id[task_id] = {
                    "id": match["id"],
                    "summary": summary,
                    "start_date": start_date,
                    "task_id": task_id,
                }
            else:
                created = (
                    service.events()
                    .insert(calendarId=calendar_id, body=event)
                    .execute()
                )
                by_task_id[task_id] = {
                    "id": created["id"],
                    "summary": summary,
                    "start_date": start_date,
                    "task_id": task_id,
                }
                counts["created"] += 1
                counts["added"] += 1
                print(f"  + created  {summary} ({start_date})")
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
        f"\n[INFO] Calendar: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged"
    )
    if counts["skipped"] or counts["failed"]:
        print(
            f"[INFO] Calendar extras — skipped: {counts['skipped']}, "
            f"failed: {counts['failed']}"
        )
    print()
    logger.info(
        "Calendar sync: created=%d updated=%d unchanged=%d skipped=%d failed=%d",
        counts["created"],
        counts["updated"],
        counts["unchanged"],
        counts["skipped"],
        counts["failed"],
    )
    return counts
