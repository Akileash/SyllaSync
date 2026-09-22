"""
Push Sylla Sync assignments to Google Calendar.

Idempotent upserts with full lifecycle:
  - Match by extendedProperties.private.task_id (sylla_sync_task_id alias)
  - Fall back to fuzzy course+title matching
  - Delete obsolete Sylla Sync events whose task_id is no longer active
  - Timed events when due time is explicit; otherwise all-day
  - Status/Priority drive colorId and summary prefixes via vocab.py
  - Skip Is Draft / phantom TBD rows
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
from course_utils import normalize_course_code
from date_utils import (
    apply_math209_online_monday_due,
    calendar_event_bounds,
    split_title_and_due,
)
from dedupe_module import (
    TASK_ID_COL,
    ensure_task_id,
    fuzzy_match_key,
    fuzzy_title_key,
    is_droppable_placeholder,
)
from retry_utils import with_retries
from vocab import calendar_color, calendar_summary_prefix, normalize_status

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar",
]

SUMMARY_RE = re.compile(r"^(?:\[(?:DONE|SUBMITTED|CANCELLED|WIP)\]\s*)?(?P<body>.+)$")
COURSE_TITLE_RE = re.compile(r"^(?P<course>.+?)\s+-\s+(?P<title>.+)$")


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
            "Settings -> Integrate calendar."
        )

    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_summary(summary: str) -> tuple[str, str]:
    """Split optional-prefix 'COURSE - Title' into (course, title)."""
    text = (summary or "").strip()
    body_match = SUMMARY_RE.match(text)
    body = body_match.group("body").strip() if body_match else text
    match = COURSE_TITLE_RE.match(body)
    if match:
        return match.group("course").strip(), match.group("title").strip()
    return "", body


def _is_syllasync_event(item: dict[str, Any]) -> bool:
    """Only touch events Sylla Sync created (description / private markers)."""
    description = item.get("description") or ""
    private = ((item.get("extendedProperties") or {}).get("private")) or {}
    return (
        "Sylla Sync" in description
        or private.get("syllasync") == "1"
        or bool(private.get("sylla_sync_task_id") or private.get("task_id"))
    )


def _event_start_key(item: dict[str, Any]) -> str:
    start = item.get("start") or {}
    if start.get("dateTime"):
        return str(start["dateTime"])
    if start.get("date"):
        return str(start["date"])
    return ""


def _is_draft_row(record: dict[str, Any]) -> bool:
    raw = record.get("Is Draft", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "draft", "tbd"}


def _build_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Sylla Sync assignment row into a Calendar event body."""
    if _is_draft_row(record):
        return None
    if normalize_status(record.get("Status")).value == "CANCELLED":
        return None

    title_col = "Assessment title" if "Assessment title" in record else "Task"
    title = str(record.get(title_col, "") or "").strip()
    course = str(record.get("Course", "") or "").strip()
    due_raw = record.get("Due Date", "")
    title, due_raw = split_title_and_due(title, due_raw)
    due_raw = apply_math209_online_monday_due(course, title, due_raw)
    # Never create events for bare "Labs" / "Assignments" placeholders
    if is_droppable_placeholder(title, due_raw):
        return None

    bounds = calendar_event_bounds(due_raw)
    if not bounds or not title:
        return None

    prefix = calendar_summary_prefix(record.get("Status"))
    base_summary = f"{course} - {title}" if course else title
    summary = f"{prefix}{base_summary}"

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
        "status": "confirmed",
        "description": (
            f"Source: Sylla Sync\n"
            f"Task ID: {task_id}\n"
            f"Priority: {priority}\n"
            f"Status: {status}\n"
            f"Estimated Time: {est_time}"
        ),
        "start": bounds["start"],
        "end": bounds["end"],
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 24 * 60}],
        },
        "extendedProperties": {
            "private": {
                "task_id": task_id,
                "sylla_sync_task_id": task_id,
                "syllasync": "1",
                "fuzzy_key": fuzzy_match_key(course, title),
                "status": status,
                "priority": priority,
            }
        },
    }

    event["colorId"] = calendar_color(status, priority)
    return event


@with_retries(label="calendar.list_events")
def _list_events_page(service, calendar_id: str, **kwargs) -> dict[str, Any]:
    return service.events().list(calendarId=calendar_id, **kwargs).execute()


def _get_existing_syllasync_events(service, calendar_id: str) -> list[dict[str, Any]]:
    """Fetch Sylla Sync-managed events in a wide date window."""
    events: list[dict[str, Any]] = []
    page_token = None
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    time_max = (now + timedelta(days=730)).isoformat().replace("+00:00", "Z")

    while True:
        result = _list_events_page(
            service,
            calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=500,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        )

        for item in result.get("items", []):
            # Never reuse trashed events — updating them leaves items invisible
            if item.get("status") == "cancelled":
                continue
            if not _is_syllasync_event(item):
                continue
            summary = (item.get("summary") or "").strip()
            course, title = _parse_summary(summary)
            private = ((item.get("extendedProperties") or {}).get("private")) or {}
            task_id = str(
                private.get("sylla_sync_task_id")
                or private.get("task_id")
                or ""
            ).strip()
            events.append(
                {
                    "id": item["id"],
                    "summary": summary,
                    "start_key": _event_start_key(item),
                    "task_id": task_id,
                    "fuzzy_key": str(private.get("fuzzy_key") or "").strip()
                    or fuzzy_match_key(course, title),
                    "status": str(private.get("status") or ""),
                    "priority": str(private.get("priority") or ""),
                    "colorId": str(item.get("colorId") or ""),
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


def _course_key_from_summary(summary: str) -> str:
    course, _ = _parse_summary(summary)
    return (normalize_course_code(course) or course or "").strip().lower()


def _title_fingerprint(summary: str) -> str:
    _, title = _parse_summary(summary)
    return fuzzy_title_key(title)


def _collect_match_candidates(
    *,
    task_id: str,
    fuzzy: str,
    summary: str,
    by_task_id: dict[str, dict[str, Any]],
    by_fuzzy: dict[str, list[dict[str, Any]]],
    existing_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Gather calendar events that likely represent the same assignment.

    Exact task_id / fuzzy_key first; then same course + same title fingerprint
    (e.g. truncated "Assignm…" vs full "Assignment One - 2026 Co-op…").
    """
    candidates: list[dict[str, Any]] = list(by_fuzzy.get(fuzzy, []))
    if task_id in by_task_id and by_task_id[task_id] not in candidates:
        candidates.append(by_task_id[task_id])

    course_key = _course_key_from_summary(summary)
    title_fp = _title_fingerprint(summary)
    if course_key and title_fp:
        for meta in existing_list:
            if meta in candidates:
                continue
            if _course_key_from_summary(meta.get("summary") or "") != course_key:
                continue
            if _title_fingerprint(meta.get("summary") or "") == title_fp:
                candidates.append(meta)
                continue
            # Prefix match only for long truncated titles (e.g. "Assignm…").
            # Short titles like HW1 must NOT match HW10 via startswith.
            _, new_title = _parse_summary(summary)
            _, old_title = _parse_summary(meta.get("summary") or "")
            a, b = new_title.lower().strip(), old_title.lower().strip()
            if (
                a
                and b
                and len(a) >= 12
                and len(b) >= 12
                and (a.startswith(b) or b.startswith(a))
            ):
                candidates.append(meta)

    # Deduplicate by event id
    by_id: dict[str, dict[str, Any]] = {}
    for c in candidates:
        by_id[c["id"]] = c
    return list(by_id.values())


@with_retries(label="calendar.delete")
def _delete_event(service, calendar_id: str, event_id: str) -> None:
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


@with_retries(label="calendar.insert")
def _insert_event(service, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return service.events().insert(calendarId=calendar_id, body=body).execute()


@with_retries(label="calendar.update")
def _update_event(
    service, calendar_id: str, event_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    return (
        service.events()
        .update(calendarId=calendar_id, eventId=event_id, body=body)
        .execute()
    )


def _events_equivalent(existing_meta: dict[str, Any], event: dict[str, Any]) -> bool:
    private = event["extendedProperties"]["private"]
    start = event.get("start") or {}
    start_key = start.get("dateTime") or start.get("date") or ""
    return (
        existing_meta.get("summary") == event.get("summary")
        and existing_meta.get("start_key") == start_key
        and existing_meta.get("task_id") == private["task_id"]
        and existing_meta.get("colorId") == str(event.get("colorId") or "")
        and existing_meta.get("status") == private.get("status", "")
        and existing_meta.get("priority") == private.get("priority", "")
    )


def compute_obsolete_event_ids(
    existing: list[dict[str, Any]],
    active_task_ids: set[str],
    keep_event_ids: set[str],
) -> list[str]:
    """
    Events tagged with Sylla Sync whose task_id is no longer active
    (and not otherwise kept as the upsert target).

    Also removes leftover category phantoms (e.g. "MATH 201 - Labs").
    """
    obsolete: list[str] = []
    for meta in existing:
        eid = meta["id"]
        if eid in keep_event_ids:
            continue
        _, title = _parse_summary(meta.get("summary") or "")
        if is_droppable_placeholder(title):
            obsolete.append(eid)
            continue
        tid = meta.get("task_id") or ""
        if tid and tid not in active_task_ids:
            obsolete.append(eid)
        elif not tid and meta.get("fuzzy_key"):
            # Untagged leftovers not matched this run → remove
            obsolete.append(eid)
    return obsolete


def push_to_google_calendar(
    df: pd.DataFrame,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Create / update / skip Google Calendar events and delete obsolete ones.

    Returns counts including deleted obsolete / duplicate events.
    """
    print(f"\n--- Google Calendar sync -> {GOOGLE_CALENDAR_ID} ---")
    if dry_run:
        print("      [dry-run] No Calendar mutations will be applied.")

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
        "obsolete_deleted": 0,
    }

    seen_task_ids: set[str] = set()
    seen_fuzzy: set[str] = set()
    keep_event_ids: set[str] = set()
    active_task_ids: set[str] = set()

    for record in df.to_dict(orient="records"):
        event = _build_event(record)
        if not event:
            counts["skipped"] += 1
            continue

        task_id = event["extendedProperties"]["private"]["task_id"]
        fuzzy = event["extendedProperties"]["private"]["fuzzy_key"]
        active_task_ids.add(task_id)

        if task_id in seen_task_ids or fuzzy in seen_fuzzy:
            counts["skipped"] += 1
            continue
        seen_task_ids.add(task_id)
        seen_fuzzy.add(fuzzy)

        summary = event["summary"]
        start = event.get("start") or {}
        start_label = start.get("dateTime") or start.get("date") or "?"

        candidates = _collect_match_candidates(
            task_id=task_id,
            fuzzy=fuzzy,
            summary=summary,
            by_task_id=by_task_id,
            by_fuzzy=by_fuzzy,
            existing_list=existing_list,
        )

        match = _prefer_keeper(candidates) if candidates else None

        try:
            event_id: str | None = None
            if match:
                for extra in candidates:
                    if extra["id"] == match["id"]:
                        continue
                    if dry_run:
                        print(f"  x [dry-run] delete duplicate {extra['summary']}")
                    else:
                        try:
                            _delete_event(service, calendar_id, extra["id"])
                            print(f"  x deleted duplicate {extra['summary']}")
                        except HttpError as del_exc:
                            if getattr(del_exc.resp, "status", None) == 410:
                                print(f"  x already gone {extra['summary']}")
                            else:
                                raise
                    counts["deleted"] += 1

                if _events_equivalent(match, event):
                    counts["unchanged"] += 1
                    print(f"  = unchanged {summary} ({start_label})")
                    event_id = match["id"]
                else:
                    if dry_run:
                        print(f"  ~ [dry-run] update {summary} ({start_label})")
                        event_id = match["id"]
                    else:
                        try:
                            _update_event(service, calendar_id, match["id"], event)
                            print(f"  ~ updated  {summary} ({start_label})")
                            event_id = match["id"]
                            counts["updated"] += 1
                        except HttpError as upd_exc:
                            # Trashed event ids return 410 — recreate fresh
                            if getattr(upd_exc.resp, "status", None) == 410:
                                created = _insert_event(service, calendar_id, event)
                                event_id = created["id"]
                                print(
                                    f"  + recreated {summary} ({start_label}) "
                                    "(previous calendar event was deleted)"
                                )
                                counts["created"] += 1
                                counts["added"] += 1
                            else:
                                raise
            else:
                if dry_run:
                    print(f"  + [dry-run] create {summary} ({start_label})")
                    event_id = f"dryrun-{task_id}"
                else:
                    created = _insert_event(service, calendar_id, event)
                    event_id = created["id"]
                    print(f"  + created  {summary} ({start_label})")
                counts["created"] += 1
                counts["added"] += 1

            assert event_id is not None
            keep_event_ids.add(event_id)
            meta = {
                "id": event_id,
                "summary": summary,
                "start_key": start_label,
                "task_id": task_id,
                "fuzzy_key": fuzzy,
                "status": event["extendedProperties"]["private"].get("status", ""),
                "priority": event["extendedProperties"]["private"].get("priority", ""),
                "colorId": str(event.get("colorId") or ""),
            }
            by_task_id[task_id] = meta
            by_fuzzy[fuzzy] = [meta]
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

    # Delete superseded / removed tasks (lifecycle)
    obsolete_ids = compute_obsolete_event_ids(
        existing_list, active_task_ids, keep_event_ids
    )
    for eid in obsolete_ids:
        try:
            summary = next(
                (m["summary"] for m in existing_list if m["id"] == eid), eid
            )
            if dry_run:
                print(f"  x [dry-run] delete obsolete {summary}")
            else:
                _delete_event(service, calendar_id, eid)
                print(f"  x deleted obsolete {summary}")
            counts["obsolete_deleted"] += 1
            counts["deleted"] += 1
        except HttpError as exc:
            logger.warning("Could not delete obsolete event %s: %s", eid, exc)

    # Near-duplicate sweep: same course + title fingerprint left outside keep set
    kept_fingerprints: set[tuple[str, str]] = set()
    for meta in existing_list:
        if meta["id"] in keep_event_ids:
            kept_fingerprints.add(
                (
                    _course_key_from_summary(meta.get("summary") or ""),
                    _title_fingerprint(meta.get("summary") or ""),
                )
            )
    for meta in existing_list:
        eid = meta["id"]
        if eid in keep_event_ids:
            continue
        fp = (
            _course_key_from_summary(meta.get("summary") or ""),
            _title_fingerprint(meta.get("summary") or ""),
        )
        if not fp[0] or not fp[1] or fp not in kept_fingerprints:
            continue
        try:
            if dry_run:
                print(f"  x [dry-run] delete near-duplicate {meta['summary']}")
            else:
                _delete_event(service, calendar_id, eid)
                print(f"  x deleted near-duplicate {meta['summary']}")
            counts["deleted"] += 1
            keep_event_ids.add(eid)  # prevent double-delete below
        except HttpError as exc:
            logger.warning("Could not delete near-duplicate %s: %s", eid, exc)

    # Final sweep: leftover fuzzy groups with >1 Sylla Sync event
    for fuzzy, group in list(by_fuzzy.items()):
        if fuzzy in seen_fuzzy:
            continue
        if len(group) <= 1:
            continue
        keeper = _prefer_keeper(group)
        for extra in group:
            if extra["id"] == keeper["id"] or extra["id"] in keep_event_ids:
                continue
            try:
                if dry_run:
                    print(f"  x [dry-run] delete orphan duplicate {extra['summary']}")
                else:
                    _delete_event(service, calendar_id, extra["id"])
                    print(f"  x deleted orphan duplicate {extra['summary']}")
                counts["deleted"] += 1
            except HttpError as exc:
                logger.warning("Could not delete duplicate %s: %s", extra["id"], exc)

    print(
        f"\n[INFO] Calendar: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged, "
        f"{counts['deleted']} deleted "
        f"({counts['obsolete_deleted']} obsolete)"
    )
    if counts["skipped"] or counts["failed"]:
        print(
            f"[INFO] Calendar extras - skipped: {counts['skipped']}, "
            f"failed: {counts['failed']}"
        )
    print()
    logger.info(
        "Calendar sync: created=%d updated=%d unchanged=%d deleted=%d "
        "obsolete=%d skipped=%d failed=%d dry_run=%s",
        counts["created"],
        counts["updated"],
        counts["unchanged"],
        counts["deleted"],
        counts["obsolete_deleted"],
        counts["skipped"],
        counts["failed"],
        dry_run,
    )
    return counts
