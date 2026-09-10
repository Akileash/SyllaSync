"""Sylla Sync — consolidate Canvas + syllabus assignments into a live dashboard."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from canvas_module import CanvasFetchError, fetch_canvas_assignments
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview Sheets/Calendar/Discord changes without mutating external state.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Continue when Canvas fails and allow downstream writes from syllabus-only "
            "data (default: abort writes on Canvas outage)."
        ),
    )
    parser.add_argument(
        "--syllabus-only",
        action="store_true",
        help="Skip Canvas assignment fetch; parse local syllabi only (implies --allow-partial).",
    )
    return parser.parse_args()


def apply_all_flag(args: argparse.Namespace) -> argparse.Namespace:
    """Expand --all into --google + --calendar + --weekly."""
    if args.all:
        args.google = True
        args.calendar = True
        args.weekly = True
    if args.syllabus_only:
        args.allow_partial = True
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

    parse_pdfs = pdf_count
    if not args.skip_canvas_download and parse_pdfs == 0:
        parse_pdfs = 1
    elif not args.skip_canvas_download:
        parse_pdfs = max(pdf_count, 1)
    low += 5 * parse_pdfs
    high += 10 * parse_pdfs

    if args.google:
        low += 2
        high += 4
    else:
        low += 2
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
    if args.dry_run:
        steps.append("DRY-RUN (no external writes)")

    mode = "full pipeline" if args.all else "pipeline"
    print(
        f"\n[INFO] Starting Sylla Sync {mode}... "
        f"Estimated completion time: ~{low}-{high} seconds."
    )
    print("[INFO] Steps: " + " -> ".join(steps))
    if args.dry_run:
        print("[INFO] Dry-run mode: Sheets / Calendar / Discord will not be mutated.")
    print()
    logger.info(
        "Pre-flight estimate ~%d-%d s | steps: %s | dry_run=%s",
        low,
        high,
        " -> ".join(steps),
        args.dry_run,
    )


def fetch_all_data(
    *,
    allow_partial: bool,
    syllabus_only: bool,
) -> tuple[list, list]:
    """
    Fetch Canvas and syllabus records.

    Canvas failures abort the pipeline unless allow_partial / syllabus_only.
    """
    canvas_data: list = []
    if syllabus_only:
        logger.warning("Syllabus-only mode: skipping Canvas assignment fetch.")
        print("      Syllabus-only: Canvas assignment fetch skipped.")
    else:
        try:
            canvas_data = fetch_canvas_assignments()
        except CanvasFetchError as exc:
            logger.error("Canvas fetch failed loudly: %s", exc)
            if not allow_partial:
                print(
                    f"[ERROR] Canvas outage / auth failure: {exc}\n"
                    "        Aborting downstream writes to avoid incomplete Sheets/"
                    "Calendar sync. Re-run with --allow-partial or --syllabus-only "
                    "to proceed with degraded data."
                )
                raise
            logger.warning(
                "Continuing with empty Canvas data (--allow-partial / --syllabus-only)."
            )
            print(f"      Warning: Canvas failed ({exc}); continuing with partial data.")
            canvas_data = []
        except Exception as exc:
            # Unexpected errors also fail loud unless partial allowed
            logger.error("Canvas fetch failed: %s", exc)
            if not allow_partial:
                raise CanvasFetchError(str(exc)) from exc
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


def export_to_google_sheet(
    canvas_data: list,
    syllabus_data: list,
    *,
    dry_run: bool = False,
):
    """
    Load existing Google Sheet data, merge new records, and push the dashboard.

    Returns:
        Prepared DataFrame written to Google Sheets.
    """
    existing = load_from_google_sheet()
    logger.info("Loaded %d existing row(s) from Google Sheet for merge.", len(existing))
    return push_from_records(
        canvas_data, syllabus_data, existing_df=existing, dry_run=dry_run
    )


def main() -> None:
    args = apply_all_flag(parse_args())
    validate_env(
        require_discord=args.weekly and not args.dry_run,
        require_google=args.google,
        require_calendar=args.calendar,
    )

    started = time.perf_counter()
    print_preflight(args)

    allowed_courses = parse_allowed_courses(ALLOWED_COURSES)
    if allowed_courses:
        logger.info("Course filter active: %s", ", ".join(allowed_courses))

    try:
        from schedule_module import enrolled_courses, lab_sections, load_schedule

        schedule = load_schedule()
        if schedule.get("sections"):
            labs = lab_sections()
            logger.info(
                "Schedule loaded (%s): %s | labs: %s",
                schedule.get("term") or "term ?",
                ", ".join(enrolled_courses()),
                ", ".join(f"{s['course']} {s['section']}" for s in labs) or "none",
            )
            print(
                f"      Schedule: {schedule.get('term')} — "
                f"{len(schedule['sections'])} section(s), "
                f"{len(labs)} lab(s)."
            )
    except Exception as exc:
        logger.warning("Could not load schedule.json: %s", exc)

    # ------------------------------------------------------------------
    # 1. Canvas API & Syllabus Extraction
    # ------------------------------------------------------------------
    print("[1/4] Canvas API & syllabus extraction...")
    logger.info("Step 1/4 — Canvas API & syllabus extraction")

    if not args.skip_canvas_download and not args.syllabus_only:
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
        except CanvasFetchError as exc:
            logger.error("Canvas syllabus download failed: %s", exc)
            if not args.allow_partial:
                print(f"[ERROR] Canvas syllabus download failed: {exc}")
                sys.exit(1)
            print(f"      Warning: syllabus download failed ({exc})")
        except Exception as exc:
            logger.warning("Canvas syllabus download failed: %s", exc)
            print(f"      Warning: syllabus download failed ({exc})")

    try:
        canvas_data, syllabus_data = fetch_all_data(
            allow_partial=args.allow_partial,
            syllabus_only=args.syllabus_only,
        )
    except CanvasFetchError:
        sys.exit(1)

    print("      Sources fetched — running dedupe engine...")

    # ------------------------------------------------------------------
    # 2. Google Sheets (--google) or local Excel dashboard
    # ------------------------------------------------------------------
    print("[2/4] Export / dashboard update...")
    logger.info("Step 2/4 — %s", "Google Sheets" if args.google else "Excel dashboard")
    try:
        if args.google:
            print(
                "      Pushing to Google Sheets..."
                + (" (dry-run)" if args.dry_run else "")
            )
            df = export_to_google_sheet(
                canvas_data, syllabus_data, dry_run=args.dry_run
            )
            print(
                f"      Google Sheets "
                f"{'previewed' if args.dry_run else 'updated'} ({len(df)} row(s))."
            )
        else:
            if args.dry_run:
                print("      [dry-run] Local Excel write skipped; merging in-memory only.")
                from dedupe_module import print_dedup_stats
                from sheets_module import incoming_from_sources, merge_tracker
                import pandas as pd

                incoming, stats = incoming_from_sources(canvas_data, syllabus_data)
                print_dedup_stats(stats)
                df = merge_tracker(pd.DataFrame(), incoming)
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
            counts = push_to_google_calendar(df, dry_run=args.dry_run)
            print(
                f"[INFO] Calendar: {counts.get('created', counts.get('added', 0))} created, "
                f"{counts.get('updated', 0)} updated, "
                f"{counts.get('unchanged', 0)} unchanged, "
                f"{counts.get('deleted', 0)} deleted"
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
            send_weekly_digest(df, dry_run=args.dry_run)
            print(
                "      Discord digest "
                + ("previewed." if args.dry_run else "sent.")
            )
        except Exception as exc:
            logger.error("Discord digest failed: %s", exc)
            print(f"[ERROR] Discord digest failed: {exc}")
            sys.exit(1)
    else:
        print("[4/4] Discord weekly digest... skipped (pass --weekly)")

    elapsed = time.perf_counter() - started
    status = "DRY-RUN complete" if args.dry_run else "SUCCESS"
    print(f"\n[{status}] Pipeline finished in {elapsed:.1f} seconds!\n")
    logger.info("Sylla Sync complete in %.1f seconds (dry_run=%s).", elapsed, args.dry_run)


if __name__ == "__main__":
    main()
