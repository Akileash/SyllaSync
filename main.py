"""Sylla Sync — consolidate Canvas + syllabus assignments into a live dashboard."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from canvas_module import fetch_canvas_assignments
from canvas_syllabus_module import download_syllabi_from_canvas
from config import ALLOWED_COURSES, BASE_DIR, EXCEL_FILE_PATH, validate_env
from course_utils import parse_allowed_courses
from discord_module import send_weekly_digest
from export_dashboard import export_dashboard
from google_sheets_module import (
    GoogleSheetPermissionError,
    load_from_google_sheet,
    push_from_records,
)
from sheets_module import update_sheet
from syllabus_module import fetch_syllabus_assignments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("syllasync")

DASHBOARD_PATH = BASE_DIR / "Sylla_Sync_Dashboard.xlsx"
ASSIGNMENTS_PATH = Path(EXCEL_FILE_PATH)
SYLLABI_DIR = BASE_DIR / "syllabi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sylla Sync — end-to-end sync of university workload to "
            "Excel/Google Sheets, Calendar, and Discord."
        )
    )
    parser.add_argument(
        "--google",
        action="store_true",
        help="Push to Google Sheets instead of exporting the local Excel dashboard.",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Push assignment due dates to Google Calendar as events.",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="After export, send a Discord digest of tasks due in the next 7 days.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Enable --google, --calendar, and --weekly together (full pipeline).",
    )
    parser.add_argument(
        "--course",
        metavar="NAME",
        help="Limit Canvas syllabus download to courses matching this name/code (e.g. 'MATH 201').",
    )
    parser.add_argument(
        "--skip-canvas-download",
        action="store_true",
        help="Do not download syllabus PDFs from Canvas before parsing.",
    )
    return parser.parse_args()


def apply_all_flag(args: argparse.Namespace) -> argparse.Namespace:
    """Expand --all into --google + --calendar + --weekly."""
    if args.all:
        args.google = True
        args.calendar = True
        args.weekly = True
    return args


def count_syllabus_pdfs() -> int:
    """Count PDF files currently in syllabi/ (used for time estimates)."""
    if not SYLLABI_DIR.is_dir():
        return 0
    return sum(1 for _ in SYLLABI_DIR.rglob("*.pdf"))


def estimate_runtime(args: argparse.Namespace, pdf_count: int) -> tuple[int, int]:
    """
    Return (low_seconds, high_seconds) estimate for the active pipeline.

    Ranges match the documented per-step budgets.
    """
    low, high = 3, 5  # Canvas API scan

    # Syllabus PDF parsing (~5–10s each); include at least 1 if download will run
    parse_pdfs = pdf_count
    if not args.skip_canvas_download and parse_pdfs == 0:
        parse_pdfs = 1  # expect at least one download + parse
    elif not args.skip_canvas_download:
        parse_pdfs = max(pdf_count, 1)
    low += 5 * parse_pdfs
    high += 10 * parse_pdfs

    if args.google:
        low += 2
        high += 4
    else:
        low += 2  # local Excel merge/dashboard
        high += 5

    if args.calendar:
        low += 3
        high += 6

    if args.weekly:
        low += 1
        high += 2

    return low, high


def print_preflight(args: argparse.Namespace) -> None:
    """Print active steps and estimated completion window before any API work."""
    pdf_count = count_syllabus_pdfs()
    low, high = estimate_runtime(args, pdf_count)

    steps = ["Canvas API scan", f"Syllabus PDF parsing ({pdf_count} file(s) on disk)"]
    if args.google:
        steps.append("Google Sheets update")
    else:
        steps.append("Local Excel dashboard")
    if args.calendar:
        steps.append("Google Calendar sync")
    if args.weekly:
        steps.append("Discord weekly digest")

    mode = "full pipeline" if args.all else "pipeline"
    print(
        f"\n[INFO] Starting Sylla Sync {mode}... "
        f"Estimated completion time: ~{low}-{high} seconds."
    )
    print("[INFO] Steps: " + " -> ".join(steps))
    print()
    logger.info(
        "Pre-flight estimate ~%d-%d s | steps: %s",
        low,
        high,
        " -> ".join(steps),
    )


def fetch_all_data() -> tuple[list, list]:
    """Fetch Canvas and syllabus records, logging and continuing on partial failure."""
    try:
        canvas_data = fetch_canvas_assignments()
    except Exception as exc:
        logger.error("Canvas fetch failed: %s", exc)
        canvas_data = []

    try:
        syllabus_data = fetch_syllabus_assignments()
    except Exception as exc:
        logger.error("Syllabus parsing failed: %s", exc)
        syllabus_data = []

    if not canvas_data and not syllabus_data:
        logger.warning("No data retrieved from any source.")

    return canvas_data, syllabus_data


def export_to_excel_dashboard(canvas_data: list, syllabus_data: list):
    """
    Merge into assignments.xlsx, then build the polished local dashboard workbook.

    Returns:
        Cleaned DataFrame written to Sylla_Sync_Dashboard.xlsx.
    """
    update_sheet(canvas_data, syllabus_data)
    logger.info("Building local dashboard at %s", DASHBOARD_PATH.resolve())
    return export_dashboard(ASSIGNMENTS_PATH, DASHBOARD_PATH)


def export_to_google_sheet(canvas_data: list, syllabus_data: list):
    """
    Load existing Google Sheet data, merge new records, and push the dashboard.

    Returns:
        Prepared DataFrame written to Google Sheets.
    """
    existing = load_from_google_sheet()
    logger.info("Loaded %d existing row(s) from Google Sheet for merge.", len(existing))
    return push_from_records(canvas_data, syllabus_data, existing_df=existing)


def main() -> None:
    args = apply_all_flag(parse_args())
    validate_env(
        require_discord=args.weekly,
        require_google=args.google,
        require_calendar=args.calendar,
    )

    started = time.perf_counter()
    print_preflight(args)

    allowed_courses = parse_allowed_courses(ALLOWED_COURSES)
    if allowed_courses:
        logger.info("Course filter active: %s", ", ".join(allowed_courses))

    # ------------------------------------------------------------------
    # 1. Canvas API & Syllabus Extraction
    # ------------------------------------------------------------------
    print("[1/4] Canvas API & syllabus extraction...")
    logger.info("Step 1/4 — Canvas API & syllabus extraction")

    if not args.skip_canvas_download:
        try:
            downloaded = download_syllabi_from_canvas(
                course_filter=args.course,
                allowed_courses=allowed_courses or None,
            )
            if downloaded:
                logger.info(
                    "Downloaded %d syllabus/schedule PDF(s) from Canvas.",
                    len(downloaded),
                )
                print(f"      Downloaded {len(downloaded)} PDF(s) from Canvas.")
        except Exception as exc:
            logger.warning("Canvas syllabus download failed: %s", exc)
            print(f"      Warning: syllabus download failed ({exc})")

    canvas_data, syllabus_data = fetch_all_data()
    print(
        f"      Merged sources — Canvas: {len(canvas_data)}, "
        f"syllabus: {len(syllabus_data)} raw record(s)."
    )

    # ------------------------------------------------------------------
    # 2. Google Sheets (--google) or local Excel dashboard
    # ------------------------------------------------------------------
    print("[2/4] Export / dashboard update...")
    logger.info("Step 2/4 — %s", "Google Sheets" if args.google else "Excel dashboard")
    try:
        if args.google:
            print("      Pushing to Google Sheets...")
            df = export_to_google_sheet(canvas_data, syllabus_data)
            print(f"      Google Sheets updated ({len(df)} row(s)).")
        else:
            print("      Building local Excel dashboard...")
            df = export_to_excel_dashboard(canvas_data, syllabus_data)
            print(f"      Excel dashboard ready ({len(df)} row(s)).")
    except GoogleSheetPermissionError as exc:
        logger.error("Google Sheets export failed: %s", exc)
        print(f"[ERROR] Google Sheets permission error: {exc}")
        sys.exit(1)
    except Exception as exc:
        target = "Google Sheets" if args.google else "Excel dashboard"
        logger.error("%s export failed: %s", target, exc)
        print(f"[ERROR] {target} export failed: {exc}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Google Calendar Sync (--calendar)
    # ------------------------------------------------------------------
    if args.calendar:
        print("[3/4] Google Calendar sync...")
        logger.info("Step 3/4 — Google Calendar sync")
        from google_calendar_module import (
            GoogleCalendarPermissionError,
            push_to_google_calendar,
        )

        try:
            counts = push_to_google_calendar(df)
            print(
                f"      Calendar — added: {counts['added']}, "
                f"updated: {counts['updated']}, "
                f"skipped: {counts['skipped']}, "
                f"failed: {counts['failed']}."
            )
        except GoogleCalendarPermissionError as exc:
            logger.error("Google Calendar permission error: %s", exc)
            print(f"[ERROR] Google Calendar permission error: {exc}")
            sys.exit(1)
        except Exception as exc:
            logger.error("Google Calendar sync failed: %s", exc)
            print(f"[ERROR] Google Calendar sync failed: {exc}")
            sys.exit(1)
    else:
        print("[3/4] Google Calendar sync... skipped (pass --calendar)")

    # ------------------------------------------------------------------
    # 4. Discord Weekly Digest (--weekly)
    # ------------------------------------------------------------------
    if args.weekly:
        print("[4/4] Discord weekly digest...")
        logger.info("Step 4/4 — Discord weekly digest")
        try:
            send_weekly_digest(df)
            print("      Discord digest sent.")
        except Exception as exc:
            logger.error("Discord digest failed: %s", exc)
            print(f"[ERROR] Discord digest failed: {exc}")
            sys.exit(1)
    else:
        print("[4/4] Discord weekly digest... skipped (pass --weekly)")

    elapsed = time.perf_counter() - started
    print(f"\n[SUCCESS] Pipeline finished in {elapsed:.1f} seconds!\n")
    logger.info("Sylla Sync complete in %.1f seconds.", elapsed)


if __name__ == "__main__":
    main()
