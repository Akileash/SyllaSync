# Sylla Sync

Sync your university assignments from **Canvas** and **syllabus PDFs** into:

| Destination | Flag |
|---|---|
| Google Sheets tracker | `--google` |
| Google Calendar due dates | `--calendar` |
| Discord 7-day digest | `--weekly` |
| Local Excel dashboard | *(default, no flags)* |

One command for everything:

```bash
python main.py --all
```

---

## Quick start (about 15 minutes)

### 1. Clone and install

```bash
git clone https://github.com/Akileash/SyllaSync.git
cd SyllaSync

python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Create your config files

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

You also need a Google **service account** key saved as `credentials.json` in this folder (see [Google setup](#google-cloud-sheets--calendar) below).  
A template lives at `credentials.json.example` — replace it with the real JSON from Google Cloud.

### 3. Fill `.env` (minimum to sync)

```env
CANVAS_URL=https://your-university.instructure.com
CANVAS_TOKEN=your_canvas_api_token

# For Google Sheets + Calendar
GOOGLE_SHEET_ID=your_google_sheet_id
GOOGLE_CALENDAR_ID=your_email@gmail.com

# Optional — leave blank to sync all active Canvas courses
ALLOWED_COURSES=MATH 201,MATH 209,ECE 202

# Optional — Discord weekly digest
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional — smarter syllabus parsing (works without this)
# GEMINI_API_KEY=your_gemini_api_key
# GEMINI_MODEL=gemini-2.5-flash

# Optional — timezone for Calendar + Discord “due today”
LOCAL_TIMEZONE=America/Edmonton
```

| Variable | Required? | Notes |
|---|---|---|
| `CANVAS_URL` | Yes | Your school’s Canvas homepage |
| `CANVAS_TOKEN` | Yes | Canvas → Account → Settings → **New Access Token** |
| `GOOGLE_SHEET_ID` | For `--google` / `--all` | ID from the spreadsheet URL |
| `GOOGLE_CALENDAR_ID` | For `--calendar` / `--all` | Usually your Gmail (not an iCal link) |
| `DISCORD_WEBHOOK_URL` | For `--weekly` / `--all` | Server → Integrations → Webhooks |
| `GEMINI_API_KEY` | No | If missing, deterministic PDF parsers are used |
| `ALLOWED_COURSES` | No | Comma-separated course codes; blank = all |
| `LOCAL_TIMEZONE` | No | Default `America/Edmonton` |
| `CANVAS_PAST_DUE_DAYS` | No | Default `21` (includes recently past-due work) |
| `TERM_YEAR` | No | Defaults to the current calendar year |

### 4. Preview, then sync

```bash
# Safe preview — no Sheets / Calendar / Discord writes
python main.py --all --dry-run

# Full sync
python main.py --all
```

---

## Google Cloud, Sheets & Calendar

Do this once.

### A. Service account + APIs

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable:
   - [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
4. **IAM & Admin → Service Accounts → Create**
5. **Keys → Add key → JSON** → save the file as `credentials.json` in the Sylla Sync folder
6. Open `credentials.json` and copy `client_email`  
   (looks like `syllasync@your-project.iam.gserviceaccount.com`)

### B. Google Sheets

1. Make a copy of the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy)
2. **Share** that copy with the service account `client_email` as **Editor**
3. Put the Sheet ID in `.env`:

   `https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit`

4. Smoke-test:

```bash
python test_sheets.py
```

### C. Google Calendar

1. [Google Calendar](https://calendar.google.com) → calendar settings → **Share with specific people**
2. Add the same `client_email`
3. Permission: **Make changes to events**
4. Set `GOOGLE_CALENDAR_ID` to your email (or a dedicated calendar ID)

---

## Optional: Discord digest

1. Discord → Server Settings → Integrations → Webhooks → **New Webhook**
2. Paste the URL into `DISCORD_WEBHOOK_URL`

The weekly digest only includes **Not Started** / **In Progress** work due in the next 7 days.  
Completed, submitted, graded, and cancelled items are skipped.

---

## Optional: `schedule.json` (section filtering)

If you are enrolled in specific lecture/lab sections, copy the example and edit it:

```bash
# Windows
copy schedule.example.json schedule.json

# macOS / Linux
cp schedule.example.json schedule.json
```

Sylla Sync uses this to ignore other Canvas sections for the same course.  
`schedule.json` is gitignored — keep your personal timetable private.

---

## Commands you’ll actually use

| Command | What it does |
|---|---|
| `python main.py --all` | Sheets + Calendar + Discord |
| `python main.py --all --dry-run` | Preview changes, write nothing external |
| `python main.py --google` | Sheets only |
| `python main.py --google --calendar` | Sheets + Calendar |
| `python main.py --weekly` | Local Excel + Discord digest |
| `python main.py --syllabus-only` | Skip Canvas assignments; parse local PDFs |
| `python main.py --allow-partial` | Continue if Canvas is down (otherwise the run aborts before writing) |
| `python main.py --skip-canvas-download` | Don’t download syllabus PDFs from Canvas |
| `python main.py --course "MATH 201"` | Limit syllabus download to one course |
| `python -m pytest` | Run unit tests |

---

## How it stays clean across runs

- Every task gets a stable **`Task_ID`** (stored on Sheets + local cache)
- Canvas assignments win over syllabus duplicates
- Course aliases collapse (`MTH 201` / `MATH 201W` → `MATH 201`)
- Bare placeholders like “Labs” / “Assignments” are dropped
- Calendar updates colors/prefixes when Status changes, and **deletes obsolete Sylla Sync events**
- Your lecture/lab calendar events are never touched
- Manual Status / Priority edits on the tracker are preserved

---

## Safety

**Never commit** these (already in `.gitignore`):

- `.env`
- `credentials.json`
- `schedule.json`
- `.syllasync_task_state.json`
- PDFs inside `syllabi/`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Missing env var | Put real values in `.env` (not the placeholder text) |
| Sheets permission error | Share the sheet with the service account email as **Editor** |
| Calendar 403 / Not Found | Enable Calendar API + share calendar as **Make changes to events** |
| Duplicate calendar events | Re-run `python main.py --google --calendar` (cleanup is automatic) |
| Canvas failure aborts the run | By design — use `--allow-partial` or `--syllabus-only` only if you accept incomplete data |
| Gemini missing / 429 | Fine — deterministic parsers still run |
| Wrong lab section synced | Add your sections to `schedule.json` |
| ModuleNotFoundError | Activate the venv, then `pip install -r requirements.txt` |
| Discord shows finished work | Mark Status as Complete / Submitted / Graded on the tracker, then re-run `--weekly` |

---

## Project layout

```text
SyllaSync/
├── main.py                   # CLI entry (--all, --dry-run, …)
├── config.py                 # Loads .env
├── vocab.py                  # Status / Priority source of truth
├── retry_utils.py            # Shared API retries (429 / 5xx)
├── dedupe_module.py          # Task_ID + anti-duplicate engine
├── canvas_module.py          # Canvas assignments
├── canvas_syllabus_module.py # Download syllabi (Modules / Files / page)
├── syllabus_module.py        # PDF parsing (Gemini optional)
├── schedule_module.py        # schedule.json section filter
├── google_sheets_module.py   # Sheets sync + Task_ID column
├── google_calendar_module.py # Calendar upsert / delete
├── discord_module.py         # Weekly digest
├── sheets_module.py          # Local Excel tracker
├── tests/                    # pytest suite
├── .env.example
├── schedule.example.json
└── credentials.json.example
```

---

## Ask an AI to set it up for you

Paste this into Cursor / ChatGPT after opening the repo:

```text
Set up Sylla Sync on my machine using this README.

1. Create a venv and install requirements.txt
2. Copy .env.example → .env
3. Help me fill .env (Canvas required; Gemini optional)
4. Walk me through Google Cloud service-account + Sheets/Calendar API setup
5. Share my Google Sheet and Calendar with the service account email
6. Optionally copy schedule.example.json → schedule.json for my sections
7. Run: python main.py --all --dry-run
8. If that looks good, run: python main.py --all
9. Confirm .env, credentials.json, and schedule.json are never committed
```
