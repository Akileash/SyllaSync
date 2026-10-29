# Sylla Sync

Sylla Sync reads Canvas assignments and syllabus PDFs, writes them to the Google Sheet **Masterlist** tab, syncs those rows to Google Calendar, and can post a Discord digest.

You can run the whole pipeline with:

```bash
python main.py --all
```

Preview the same pipeline without writing to the Sheet, Calendar, or Discord:

```bash
python main.py --all --dry-run
```

Check that configuration is present without calling any service and without printing secret values:

```bash
python main.py --check
```

## What it writes

Sheet and Excel statuses are only:

- Not Started
- In Progress
- Submitted
- Graded
- Cancelled

**Submitted** is the finished status. The program never writes the word Complete. Submitted calendar events use the title prefix `[SUBMITTED] `.

Columns on the Masterlist tab stay A–H:

| Column | Field |
| --- | --- |
| A | Task_ID |
| B | Status |
| C | Due date |
| D | Due date used by the template formulas |
| E | Time |
| F | Class |
| G | Type |
| H | Assignment |

Columns I and after are template formulas. Sylla Sync does not overwrite them.

Calendar sync only changes events Sylla Sync created (the description contains "Sylla Sync", or the private property `syllasync=1`). A normal run still removes Sylla Sync events whose task is gone. If Canvas or the Sheet read fails, that delete step stays off so a partial fetch cannot remove events. Dry-run never writes and never deletes.

## Prerequisites

- Python 3.10 or newer (`python --version`)
- A Canvas account and a personal access token
- A Google Cloud project with the Google Sheets API and Google Calendar API enabled
- A copy of the [HHS Student Life @ Purdue assignment tracker](https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK) (or [make a copy](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy))
- Optional: a Gemini API key, for PDFs the built-in parser does not cover
- Optional: a Discord channel webhook

The built-in parser covers common syllabus and weekly-schedule PDFs. Gemini is optional. If Gemini is down (it often returns HTTP 503, "high demand"), that PDF is skipped and Canvas rows still sync.

## Install

From the project folder.

### Windows

Use the same folder you cloned, including a OneDrive path. Close Excel if `assignments.xlsx` is open. If OneDrive shows a cloud-only file, wait until it is available offline.

```bat
py -3 -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
copy schedule.example.json schedule.json
```

Save `.env` and `schedule.json` as UTF-8. Notepad's "UTF-8" encoding is fine. A byte-order mark (BOM) and Windows CRLF line endings are accepted.

If you paste a path into `.env`, quotes are stripped. Example:

```text
EXCEL_FILE_PATH="C:\Users\me\OneDrive\School\SyllaSync\assignments.xlsx"
```

The console is switched to UTF-8 when the program starts, so course titles outside the legacy Windows code page do not crash the run.

### macOS and Linux

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
cp schedule.example.json schedule.json
```

## Configuration

Put secrets in `.env` or `local.env` in the project folder. `local.env` overrides `.env`. Both files are gitignored. `.env.example` contains placeholders only.

| Variable | Required | Purpose |
| --- | --- | --- |
| `CANVAS_URL` | Yes, except `--syllabus-only` | School Canvas host, such as `https://school.instructure.com` |
| `CANVAS_TOKEN` | Yes, except `--syllabus-only` | Canvas access token |
| `GOOGLE_SHEET_ID` | For `--google` / `--all` | ID from the Sheet URL |
| `GOOGLE_CALENDAR_ID` | For `--calendar` / `--all` | Gmail address or calendar ID. Not an iCal / `.ics` link |
| `DISCORD_WEBHOOK_URL` | For `--weekly` / `--all` | Discord webhook URL |
| `GEMINI_API_KEY` | No | Enables Gemini PDF parsing |
| `GEMINI_MODEL` | No | Defaults to `gemini-3.6-flash` |
| `ALLOWED_COURSES` | No | Comma-separated codes. Blank includes every active Canvas course |
| `LOCAL_TIMEZONE` | No | IANA name. Default `America/Edmonton` |
| `TERM_YEAR` | No | Four-digit year. Default is the current calendar year |
| `CANVAS_PAST_DUE_DAYS` | No | How many days of past-due Canvas work to keep. Default `21` |
| `EXCEL_FILE_PATH` | No | Local workbook path. Default `assignments.xlsx` |

`credentials.json` (the Google service-account key) sits in the project folder and is gitignored. Do not commit `.env`, `local.env`, `credentials.json`, `schedule.json`, `.syllasync_task_state.json`, or files under `syllabi/`.

`python main.py --check` lists missing or placeholder settings by name. It does not print token, key, webhook, or private-key values, and it does not contact Canvas, Google, Gemini, or Discord.

## Google Cloud (Sheets and Calendar)

Sylla Sync signs in with a **service account** JSON key (OAuth 2.0 JWT). It does not open a browser login, and it does not use a desktop OAuth client file.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Enable **Google Sheets API** and **Google Calendar API** (APIs & Services → Library).
3. APIs & Services → Credentials → Create credentials → Service account. Give it a name and finish the wizard. No extra roles are required on the project.
4. Open the service account → Keys → Add key → JSON. Save the download as `credentials.json` in this folder.
5. Open `credentials.json` and copy only the `client_email` value. You will share the Sheet and the calendar with that address. Do not share the `private_key`.
6. Make your own copy of the HHS tracker. Copy the ID between `/d/` and `/edit` in the Sheet URL into `GOOGLE_SHEET_ID`.
7. In the Sheet, click Share, paste `client_email`, and set the role to **Editor**.
8. In Google Calendar, open the calendar you want to use → Settings and sharing → Share with specific people → add `client_email` with **Make changes to events**.
9. Set `GOOGLE_CALENDAR_ID` to the Gmail address that owns that calendar, or to the ID under Integrate calendar. Do not paste a secret iCal address.

`credentials.json.example` shows the key shape with placeholders. Replace the file with the download from Google Cloud. Do not fill the example in by hand.

## Canvas token

1. In Canvas, open Account → Settings.
2. Under Approved Integrations, choose **New Access Token**.
3. Put the token in `CANVAS_TOKEN` and your school's host in `CANVAS_URL`.
4. If a later run says the token was rejected, create a new token. Canvas tokens expire or get revoked. The old value is not printed.

`ALLOWED_COURSES` limits which courses are read, for example:

```text
ALLOWED_COURSES=MATH 201,MATH 209,MAT E 201,ECE 202,ECE 210,ENGG 299
```

## Gemini key

1. Create a key in [Google AI Studio](https://aistudio.google.com/apikey).
2. Set `GEMINI_API_KEY` in `.env`.
3. Leave it unset to use only the built-in PDF parser.

A 503 or "high demand" response is retried with backoff. If the model still fails, or the reply is not usable JSON, that PDF is skipped. Other PDFs and Canvas rows continue. Partial JSON is kept when complete assignment objects are present.

## Discord webhook

1. In the Discord channel, open Edit Channel → Integrations → Webhooks → New Webhook.
2. Copy the webhook URL into `DISCORD_WEBHOOK_URL`.
3. If Discord later returns "rejected", the webhook was deleted or regenerated. Create a new one. The URL is not printed.

The digest lists work due in the next 7 days in `LOCAL_TIMEZONE`. Submitted, Graded, and Cancelled rows are left out.

## schedule.json

Copy `schedule.example.json` to `schedule.json`. For each of your sections, set `course`, `type` (`LEC`, `LAB`, or `SEM`), `section`, `days`, `start`, `end`, and `location`.

This file keeps Canvas lab and lecture shells that are not yours (for example another lab section) out of the sync. If the file is missing, section filtering is off and active courses still import.

`schedule.json` is gitignored. Its format is a JSON object with a `sections` array. Do not rename those fields.

## Commands

Run these from the project folder with the virtual environment activated.

| Command | What it does |
| --- | --- |
| `python main.py --check` | Validate configuration. Alias: `--validate`. Exit 1 when something required is missing or still a placeholder |
| `python main.py --check --all` | Same check, and require Sheet, Calendar, and Discord settings |
| `python main.py --all --dry-run` | Run the full pipeline without writing to Sheets, Calendar, or Discord |
| `python main.py --all` | Sheet + Calendar + Discord |
| `python main.py --google` | Update the Google Sheet only |
| `python main.py --google --calendar` | Sheet and Calendar |
| `python main.py --calendar` | Calendar only, using the local Excel merge |
| `python main.py --weekly` | Discord digest only, using the local Excel merge |
| `python main.py --course "MATH 201"` | Limit the Canvas syllabus download to a matching course name or code |
| `python main.py --skip-canvas-download` | Do not download new PDFs; parse what is already in `syllabi/` |
| `python main.py --syllabus-only` | Skip Canvas. Implies `--allow-partial` |
| `python main.py --allow-partial` | Continue after a Canvas failure. Sheet writes use what is left. Calendar deletes stay off |
| `python main.py` | Local Excel dashboard only (no Google, Calendar, or Discord) |
| `python test_sheets.py` | Live Sheet test. This writes `Connection Successful!` into Masterlist cell A1. Skip it unless you mean to |
| `python export_dashboard.py` | Rebuild `Sylla_Sync_Dashboard.xlsx` from `assignments.xlsx` |
| `python -m pytest tests/ -q` | Run the test suite |

`--dry-run` still contacts Canvas, Google, or Discord when those settings look real, so the preview can show creates and updates. It does not mutate them. Placeholder or missing settings are skipped instead of failing the preview. `--check` never contacts them.

A failed Discord post does not undo the Sheet or Calendar. A failed Calendar sync does not undo the Sheet. A failed Sheet write does not stop Calendar or Discord from using the rows already fetched. A Canvas failure on a normal run stops before any write, so existing rows and events stay as they are. Pass `--allow-partial` to continue from the other source; deletes stay off in that case.

## Syllabus PDFs

PDFs are stored in `syllabi/`. Names that contain "syllabus" or "schedule" are parsed locally. Other PDFs are sent to Gemini when a key is set. One bad or locked PDF does not stop the rest. Encrypted PDFs need an unlocked export. If OneDrive is still downloading a file, wait and run again.

## Tests

```bash
python -m pytest tests/ -q
```

The tests use fake data. They do not call your Canvas, Google, or Discord accounts.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `--check` lists a missing name | Set that name in `.env` or `local.env`. The value is not printed |
| "still the example placeholder" | Replace the sample from `.env.example`. Do not commit the real file |
| Canvas rejected `CANVAS_TOKEN` | Create a new token under Canvas Account → Settings. Confirm `CANVAS_URL` is your school's host |
| `credentials.json` could not be read | Download a new service-account JSON key. Do not use an OAuth desktop client file |
| Google rejected the service account / refresh failed | The key was revoked or the clock is wrong. Download a new key. Private key text is not logged |
| Sheet permission error or spreadsheet not found | Share the Sheet with `client_email` as Editor. Confirm `GOOGLE_SHEET_ID` and that the tab is named `Masterlist` |
| Calendar permission error | Share the calendar as "Make changes to events". `GOOGLE_CALENDAR_ID` must be an email or calendar ID, not an iCal URL |
| Calendar HTTP 410 on an event | The event was trashed. The next full sync creates a new confirmed event |
| Discord rejected the webhook | Create a new webhook and update `.env`. The URL is not logged |
| Gemini 503 / high demand | Wait and re-run. Other sources still sync. Or unset `GEMINI_API_KEY` and use the built-in parser |
| Gemini returned malformed JSON | That PDF is skipped. The raw reply is not logged |
| `ModuleNotFoundError` | Activate the virtual environment and run `pip install -r requirements.txt` |
| Excel file locked / OneDrive | Close the workbook and wait for OneDrive to finish syncing, then re-run |
| Odd characters or a crash on print (Windows) | Keep the files UTF-8. The program asks the console for UTF-8 |
| First key in `.env` is ignored | The file had a BOM on an older setup. Current versions accept a UTF-8 BOM |
| Wrong lab section | Fix `section` in `schedule.json` and re-run |
| On the Sheet but not on the calendar | The due date may be blank. Calendar skips rows with no real date. It does not invent one |
| HW1 updated HW10 | Title matching does not treat a short code as a prefix of a longer one |

## Layout

| File | Role |
| --- | --- |
| `main.py` | Command line and pipeline |
| `config.py` | `.env` / `local.env` and `--check` |
| `canvas_module.py` | Canvas assignments |
| `canvas_syllabus_module.py` | Syllabus PDF download |
| `syllabus_module.py` | PDF parsing |
| `dedupe_module.py` | Task_ID and cross-source dedupe |
| `google_sheets_module.py` | Masterlist read and write |
| `google_calendar_module.py` | Calendar upsert |
| `discord_module.py` | Weekly digest |
| `schedule_module.py` | `schedule.json` |
| `sheets_module.py` | Local Excel tracker |
| `vocab.py` | Status labels |
| `safe_text.py` | UTF-8, paths, and redaction |
| `retry_utils.py` | Backoff for 429 / 5xx, including Gemini 503 |
| `tests/` | Regression tests |

## Credits

Google Sheets assignment tracker template by **HHS Student Life @ Purdue**:  
https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK
