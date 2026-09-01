# Sylla Sync

Sync university assignments from **Canvas** and **syllabus PDFs** into an Excel dashboard or a **Google Sheets** assignment tracker. Optional **Discord** weekly digest included.

**Requirements:** Python 3.10+, a Canvas account, and (for Google Sheets) a Google account.

## Quick start

```bash
git clone <your-repo-url>
cd SyllaSync
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # Windows: copy .env.example .env
cp credentials.json.example credentials.json
```

1. Fill in `.env` with your own API keys (see below).
2. For Google Sheets: copy the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy), create a service account, save the key as `credentials.json`, and share your sheet with the service account email as **Editor**.
3. Test Google Sheets: `python test_sheets.py`
4. Run a sync: `python main.py --google`

## Commands

| Command | Output |
|---|---|
| `python main.py` | Local Excel → `Sylla_Sync_Dashboard.xlsx` |
| `python main.py --google` | Push to Google Sheet **Masterlist** tab |
| `python main.py --weekly` | Excel export + Discord 7-day digest |
| `python main.py --google --weekly` | Google Sheet + Discord digest |
| `python main.py --google --course "MATH 201"` | Download syllabi for one course, then sync |
| `python main.py --google --skip-canvas-download` | Sync local `syllabi/` PDFs only |
| `python test_sheets.py` | Verify Google Sheets connection |

## Environment variables

Copy `.env.example` → `.env` and fill in your values:

| Variable | Required | Description |
|---|---|---|
| `CANVAS_URL` | Yes | Your school's Canvas URL (e.g. `https://canvas.university.edu`) |
| `CANVAS_TOKEN` | Yes | Canvas → **Account → Settings → Approved Integrations** |
| `GEMINI_API_KEY` | Yes* | [Google AI Studio](https://aistudio.google.com/apikey) — for PDF parsing |
| `GOOGLE_SHEET_ID` | For `--google` | The ID from your sheet URL: `docs.google.com/spreadsheets/d/SHEET_ID/edit` |
| `DISCORD_WEBHOOK_URL` | For `--weekly` | Discord server webhook URL |

\*Structured syllabus/schedule PDFs (e.g. weekly schedules) are parsed without Gemini. Gemini is used as a fallback for other PDF formats.

## Google Sheets setup

Targets the **Masterlist** tab of the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy) template (free, by [@HHSStudentLife](https://linktr.ee/hhsstudentlife)).

1. Open the template link above and click **Make a copy**.
2. [Create a Google Cloud service account](https://console.cloud.google.com/iam-admin/serviceaccounts) and download the JSON key → save as `credentials.json`.
3. Share your copy of the sheet with the `client_email` from that file (Editor access).
4. Copy the sheet ID from the URL into `GOOGLE_SHEET_ID` in `.env`.
5. Run `python test_sheets.py` to confirm.

**What Sylla Sync writes (columns B–H):**

| Column | Content |
|---|---|
| B — STATUS | Defaults to `Not Started` (preserved on re-sync) |
| C/D — DUE DATE | Calendar date (column D feeds template formulas) |
| E — DUE TIME | Time if available |
| F — CLASS | Course code/name |
| G — TYPE | Auto-inferred (Exam, Quiz, Homework, etc.) |
| H — ASSIGNMENT | Title |

Columns **I onward** (formulas, charts) are never modified.

## Syllabus PDFs

**Option A — Manual:** Drop PDFs into `syllabi/`.

**Option B — From Canvas:** Sylla Sync auto-downloads `Syllabus.pdf` and weekly schedule files from course modules on each run. Use `--course "MATH 201"` to limit downloads to one course.

Parsed assignments are merged with Canvas data. Re-syncing preserves your manual **Status**, **Priority**, and **Estimated time** edits.

## Discord digest (optional)

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**
2. Name it (e.g. "Sylla Sync"), pick a channel, and copy the webhook URL
3. Add it to `.env` as `DISCORD_WEBHOOK_URL`, then run `python main.py --google --weekly`

## Troubleshooting

| Problem | Fix |
|---|---|
| `Missing required environment variable` | Check `.env` — values must not be placeholder text |
| `Permission denied` on Google Sheets | Share your sheet with the service account `client_email` as Editor |
| `Worksheet 'Masterlist' not found` | Use the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy) template |
| `Canvas fetch failed` | Verify `CANVAS_URL` and `CANVAS_TOKEN`; classes may not be published yet |
| `429` / quota error (Gemini) | Free tier limit hit — wait 24h or rely on structured PDF parsers |
| `No PDF files found` | Add PDFs to `syllabi/` or run without `--skip-canvas-download` |
| `ModuleNotFoundError` | Activate venv, then `pip install -r requirements.txt` |
| Discord digest not sending | Check `DISCORD_WEBHOOK_URL` in `.env` and run with `--weekly` |

## Project layout

```
SyllaSync/
├── main.py                   # CLI entry point
├── canvas_module.py          # Canvas assignments
├── canvas_syllabus_module.py # Download syllabi from Canvas modules
├── syllabus_module.py        # PDF parsing (regex + Gemini)
├── google_sheets_module.py   # Google Sheets → Masterlist
├── sheets_module.py          # Excel merge writer
├── export_dashboard.py       # Excel dashboard formatter
├── discord_module.py         # Discord webhook digest
├── config.py                 # Loads .env
├── .env.example              # Template
├── credentials.json.example  # Template
└── syllabi/                  # Syllabus PDFs
```
