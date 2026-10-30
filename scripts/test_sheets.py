"""
Quick test to verify Google Sheets connectivity for Sylla Sync.

By default this only reads Masterlist cell A1. Pass --write to store
"Connection Successful!" in that cell.

Usage:
    python scripts/test_sheets.py
    python scripts/test_sheets.py --write
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from gspread.exceptions import APIError, SpreadsheetNotFound

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from syllasync.config import (  # noqa: E402
    _credentials_issues,
    is_placeholder,
    warn_if_tzdata_old,
)
CREDENTIALS_PATH = BASE_DIR / "credentials.json"

# Load environment variables (.env first, then local.env override)
if os.getenv("SYLLASYNC_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "yes"}:
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR / "local.env", override=True)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that the service account can open the Masterlist tab."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write 'Connection Successful!' into Masterlist cell A1. Omit this to stay read-only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    warn_if_tzdata_old()
    if not GOOGLE_SHEET_ID or is_placeholder(GOOGLE_SHEET_ID):
        print(
            "Error: GOOGLE_SHEET_ID is missing or still the example placeholder.\n"
            "Set it in your .env or local.env file, then run: python main.py --check",
            file=sys.stderr,
        )
        sys.exit(1)

    if not CREDENTIALS_PATH.exists():
        print(
            f"Error: credentials.json not found at {CREDENTIALS_PATH}\n"
            "Download your service account key from Google Cloud Console.",
            file=sys.stderr,
        )
        sys.exit(1)

    cred_errors = [i for i in _credentials_issues(required=True) if i.level == "error"]
    if cred_errors:
        for issue in cred_errors:
            print(f"Error: {issue.message}", file=sys.stderr)
        print("Fix credentials.json, then run: python main.py --check", file=sys.stderr)
        sys.exit(1)

    try:
        client = gspread.service_account(
            filename=str(CREDENTIALS_PATH),
            scopes=SCOPES,
        )
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = spreadsheet.worksheet("Masterlist")
        if args.write:
            worksheet.update_acell("A1", "Connection Successful!")
            print("Wrote 'Connection Successful!' to Masterlist A1.")
        else:
            print("Read-only check. Pass --write to update Masterlist cell A1.")

        print("Connection Successful!")
        print(f"  Sheet title : {spreadsheet.title}")
        print(f"  Worksheet   : {worksheet.title} (Masterlist)")
        print(f"  Cell A1     : {worksheet.acell('A1').value}")
        print()
        print("Ready to sync with: python main.py --google")

    except SpreadsheetNotFound:
        print(
            "Error: Spreadsheet not found.\n"
            "  - Check that GOOGLE_SHEET_ID in .env is correct\n"
            "  - Share the sheet with your service account email as Editor",
            file=sys.stderr,
        )
        sys.exit(1)

    except APIError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        if status in (403, 404):
            print(
                "Error: Permission denied.\n"
                "  - Open credentials.json and copy the client_email value\n"
                "  - In Google Sheets: Share → paste that email → Editor access",
                file=sys.stderr,
            )
        else:
            print(f"Error: Google Sheets API error ({status}): {exc}", file=sys.stderr)
        sys.exit(1)

    except FileNotFoundError as exc:
        print(f"Error: Could not read credentials.json: {exc}", file=sys.stderr)
        sys.exit(1)

    except Exception as exc:
        print(f"Error: Unexpected failure: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
