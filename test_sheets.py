"""
Quick test to verify Google Sheets connectivity for Sylla Sync.

Usage:
    python test_sheets.py
"""

import os
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from gspread.exceptions import APIError, SpreadsheetNotFound

BASE_DIR = Path(__file__).parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"

# Load environment variables (.env first, then local.env override)
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / "local.env", override=True)

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def main() -> None:
    if not GOOGLE_SHEET_ID:
        print(
            "Error: GOOGLE_SHEET_ID is not set.\n"
            "Add it to your .env or local.env file.",
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

    try:
        client = gspread.service_account(
            filename=str(CREDENTIALS_PATH),
            scopes=SCOPES,
        )
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        worksheet = spreadsheet.worksheet("Masterlist")
        worksheet.update_acell("A1", "Connection Successful!")

        print("Connection Successful!")
        print(f"  Sheet title : {spreadsheet.title}")
        print(f"  Worksheet   : {worksheet.title} (Masterlist)")
        print(f"  Cell A1     : {worksheet.acell('A1').value}")
        print()
        print("Ready to sync with: python main.py --google")

    except SpreadsheetNotFound as exc:
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
