"""Sylla Sync — consolidate Canvas + syllabus assignments into a live dashboard."""

import argparse
import logging
import sys
from pathlib import Path

from canvas_module import fetch_canvas_assignments
from canvas_syllabus_module import download_syllabi_from_canvas
from config import BASE_DIR, EXCEL_FILE_PATH, validate_env
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sylla Sync — sync university workload to Excel or Google Sheets."
    )
    parser.add_argument(
        "--google",
        action="store_true",
        help="Push to Google Sheets instead of exporting the local Excel dashboard.",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="After export, send a Discord digest of tasks due in the next 7 days.",
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
    args = parse_args()
    validate_env(require_discord=args.weekly, require_google=args.google)

    logger.info("Starting Sylla Sync...")

    if not args.skip_canvas_download:
        try:
            downloaded = download_syllabi_from_canvas(course_filter=args.course)
            if downloaded:
                logger.info(
                    "Downloaded %d syllabus/schedule PDF(s) from Canvas.",
                    len(downloaded),
                )
        except Exception as exc:
            logger.warning("Canvas syllabus download failed: %s", exc)

    canvas_data, syllabus_data = fetch_all_data()

    # --- Export to Excel (default) or Google Sheets (--google) ---
    try:
        if args.google:
            logger.info("Export target: Google Sheets")
            df = export_to_google_sheet(canvas_data, syllabus_data)
        else:
            logger.info("Export target: local Excel dashboard")
            df = export_to_excel_dashboard(canvas_data, syllabus_data)
    except GoogleSheetPermissionError as exc:
        logger.error("Google Sheets export failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        target = "Google Sheets" if args.google else "Excel dashboard"
        logger.error("%s export failed: %s", target, exc)
        sys.exit(1)

    # --- Optional Discord digest ---
    if args.weekly:
        try:
            send_weekly_digest(df)
        except Exception as exc:
            logger.error("Discord digest failed: %s", exc)
            sys.exit(1)

    logger.info("Sylla Sync complete.")


if __name__ == "__main__":
    main()
