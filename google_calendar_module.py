"""
Push Sylla Sync assignments to Google Calendar as all-day events.

Idempotent upserts:
  - Prefer matching by extendedProperties.private.task_id
  - Fall back to fuzzy course+title matching (Assignment #1 Long vs Short)
  - Delete leftover Sylla Sync duplicate events for the same fuzzy key
  - Only manage events whose description contains "Sylla Sync"
    (never touches lecture/lab schedule events)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import BASE_DIR, GOOGLE_CALENDAR_ID
from date_utils import normalize_calendar_date
from dedupe_module import TASK_ID_COL, ensure_task_id, fuzzy_match_key

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

SUMMARY_RE = re.compile(r"^(?P<course>.+?)\s+-\s+(?P<title>.+)$")


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


def _parse_summary(summary: str) -> tuple[str, str]:
    """Split 'COURSE - Title' into (course, title)."""
    text = (summary or "").strip()
    match = SUMMARY_RE.match(text)
    if match:
        return match.group("course").strip(), match.group("title").strip()
    return "", text


def _is_syllasync_event(item: dict[str, Any]) -> bool:
    """Only touch events Sylla Sync created (description marker)."""
    description = item.get("description") or ""
    private = ((item.get("extendedProperties") or {}).get("private")) or {}
    return "Sylla Sync" in description or private.get("syllasync") == "1"


def _event_start_date(item: dict[str, Any]) -> str:
    start = item.get("start") or {}
    if start.get("date"):
        return start["date"]
    if start.get("dateTime"):
        return str(start["dateTime"])[:10]
    return ""


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
                "fuzzy_key": fuzzy_match_key(course, title),
            }
        },
    }

    color = _priority_color(priority)
    if color:
        event["colorId"] = color

    return event


def _get_existing_syllasync_events(service, calendar_id: str) -> list[dict[str, Any]]:
    """Fetch Sylla Sync-managed events in a wide date window."""
    events: list[dict[str, Any]] = []
    page_token = None
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    time_max = (now + timedelta(days=730)).isoformat().replace("+00:00", "Z")

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
            if not _is_syllasync_event(item):
                continue
            summary = (item.get("summary") or "").strip()
            course, title = _parse_summary(summary)
            private = ((item.get("extendedProperties") or {}).get("private")) or {}
            events.append(
                {
                    "id": item["id"],
                    "summary": summary,
                    "start_date": _event_start_date(item),
                    "task_id": str(private.get("task_id") or "").strip(),
                    "fuzzy_key": str(private.get("fuzzy_key") or "").strip()
                    or fuzzy_match_key(course, title),
                    "raw": item,
                }
            )

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def _prefer_keeper(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer event with task_id, then longest summary."""
    return sorted(
        candidates,
        key=lambda e: (
            1 if e.get("task_id") else 0,
            len(e.get("summary") or ""),
        ),
        reverse=True,
    )[0]


def _delete_event(service, calendar_id: str, event_id: str) -> None:
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def _events_equivalent(existing_meta: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        existing_meta.get("summary") == event.get("summary")
        and existing_meta.get("start_date") == (event.get("start") or {}).get("date")
        and existing_meta.get("task_id")
        == event["extendedProperties"]["private"]["task_id"]
    )


def push_to_google_calendar(df: pd.DataFrame) -> dict[str, int]:
    """
    Create / update / skip Google Calendar events and remove Sylla Sync duplicates.

    Returns counts including deleted duplicate events.
    """
    print(f"\n--- Google Calendar sync -> {GOOGLE_CALENDAR_ID} ---")

    try:
        service = _authenticate()
        calendar_id = GOOGLE_CALENDAR_ID
        existing_list = _get_existing_syllasync_events(service, calendar_id)
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

    by_task_id: dict[str, dict[str, Any]] = {}
    by_fuzzy: dict[str, list[dict[str, Any]]] = {}
    for meta in existing_list:
        if meta["task_id"]:
            by_task_id.setdefault(meta["task_id"], meta)
        if meta["fuzzy_key"]:
            by_fuzzy.setdefault(meta["fuzzy_key"], []).append(meta)

    print(
        f"Found {len(existing_list)} Sylla Sync event(s) "
        f"({len(by_task_id)} with task_id, {len(by_fuzzy)} fuzzy groups)."
    )

    counts = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "deleted": 0,
        "added": 0,
    }

    seen_task_ids: set[str] = set()
    seen_fuzzy: set[str] = set()
    keep_event_ids: set[str] = set()

    for record in df.to_dict(orient="records"):
        event = _build_event(record)
        if not event:
            counts["skipped"] += 1
            continue

        task_id = event["extendedProperties"]["private"]["task_id"]
        fuzzy = event["extendedProperties"]["private"]["fuzzy_key"]
        if task_id in seen_task_ids or fuzzy in seen_fuzzy:
            counts["skipped"] += 1
            continue
        seen_task_ids.add(task_id)
        seen_fuzzy.add(fuzzy)

        summary = event["summary"]
        start_date = event["start"]["date"]

        candidates = list(by_fuzzy.get(fuzzy, []))
        if task_id in by_task_id and by_task_id[task_id] not in candidates:
            candidates.append(by_task_id[task_id])

        match = _prefer_keeper(candidates) if candidates else None

        try:
            if match:
                # Delete other fuzzy duplicates for this assignment
                for extra in candidates:
                    if extra["id"] == match["id"]:
                        continue
                    _delete_event(service, calendar_id, extra["id"])
                    counts["deleted"] += 1
                    print(f"  x deleted duplicate {extra['summary']}")

                if _events_equivalent(match, event):
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

                keep_event_ids.add(match["id"])
                by_task_id[task_id] = {
                    "id": match["id"],
                    "summary": summary,
                    "start_date": start_date,
                    "task_id": task_id,
                    "fuzzy_key": fuzzy,
                }
                by_fuzzy[fuzzy] = [by_task_id[task_id]]
            else:
                created = (
                    service.events()
                    .insert(calendarId=calendar_id, body=event)
                    .execute()
                )
                keep_event_ids.add(created["id"])
                meta = {
                    "id": created["id"],
                    "summary": summary,
                    "start_date": start_date,
                    "task_id": task_id,
                    "fuzzy_key": fuzzy,
                }
                by_task_id[task_id] = meta
                by_fuzzy[fuzzy] = [meta]
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

    # Final sweep: any leftover fuzzy groups with >1 Sylla Sync event
    for fuzzy, group in list(by_fuzzy.items()):
        if fuzzy in seen_fuzzy:
            continue
        if len(group) <= 1:
            continue
        keeper = _prefer_keeper(group)
        for extra in group:
            if extra["id"] == keeper["id"]:
                continue
            try:
                _delete_event(service, calendar_id, extra["id"])
                counts["deleted"] += 1
                print(f"  x deleted orphan duplicate {extra['summary']}")
            except HttpError as exc:
                logger.warning("Could not delete duplicate %s: %s", extra["id"], exc)

    print(
        f"\n[INFO] Calendar: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged, "
        f"{counts['deleted']} duplicates deleted"
    )
    if counts["skipped"] or counts["failed"]:
        print(
            f"[INFO] Calendar extras - skipped: {counts['skipped']}, "
            f"failed: {counts['failed']}"
        )
    print()
    logger.info(
        "Calendar sync: created=%d updated=%d unchanged=%d deleted=%d skipped=%d failed=%d",
        counts["created"],
        counts["updated"],
        counts["unchanged"],
        counts["deleted"],
        counts["skipped"],
        counts["failed"],
    )
    return counts
