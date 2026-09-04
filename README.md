# Sylla Sync

Sync university assignments from **Canvas** + **syllabus PDFs** into:

- Google Sheets (assignment tracker)
- Google Calendar (due-date events)
- Discord (7-day digest)
- or a local Excel dashboard

**Vibe coded** with Cursor.

Paste this README into an AI coding assistant and ask it to set Sylla Sync up for you.

---

## What you need before starting

| Item | Where to get it |
|---|---|
| Python 3.10+ | https://www.python.org/downloads/ |
| Canvas API token | Canvas → Account → Settings → **New Access Token** |
| Gemini API key (optional but useful) | https://aistudio.google.com/apikey |
| Google account | for Sheets + Calendar |
| Discord webhook (optional) | Server Settings → Integrations → Webhooks |

---

## AI setup prompt (copy/paste)

```text
Set up Sylla Sync on my machine using this README.

Do these steps in order:
1. Create a venv and install requirements.txt
2. Copy .env.example → .env and credentials.json.example → credentials.json
3. Help me fill .env with my Canvas / Gemini / Google / Discord values
4. Walk me through Google Cloud service-account + Calendar API setup
5. Share my Google Sheet and Calendar with the service account email
6. Run: python test_sheets.py
7. Run: python main.py --all
8. Confirm .env and credentials.json are gitignored and never committed
```

---

## 1) Install

```bash
git clone https://github.com/Akileash/SyllaSync.git
cd SyllaSync

python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
```

Copy templates:

```bash
# Windows
copy .env.example .env
copy credentials.json.example credentials.json

# macOS/Linux
cp .env.example .env
cp credentials.json.example credentials.json
```

---

## 2) Fill `.env`

Open `.env` and replace placeholders:

```env
CANVAS_URL=https://your-university.instructure.com
CANVAS_TOKEN=your_canvas_api_token
GEMINI_API_KEY=your_gemini_api_key
GOOGLE_SHEET_ID=your_google_sheet_id
GOOGLE_CALENDAR_ID=your_email@gmail.com
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ALLOWED_COURSES=MATH 201,MATH 209,MAT E 201,ECE 202,ECE 210,ENGG 299
```

Tips:

- `CANVAS_URL` is your school Canvas homepage URL
- `ALLOWED_COURSES` filters which classes sync (leave blank for all active courses)
- `GOOGLE_CALENDAR_ID` is usually your Gmail address (not an iCal URL)

---

## 3) Google Cloud + credentials.json (one-time)

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create/select a project
3. Enable both APIs:
   - [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
4. Create a **service account** → Keys → Add key → JSON
5. Save the downloaded JSON as `credentials.json` in the project root
6. Open `credentials.json` and copy `client_email` (looks like `name@project.iam.gserviceaccount.com`)

---

## 4) Google Sheets setup

1. Make a copy of the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy)
2. Share that copy with your service account `client_email` as **Editor**
3. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit`
4. Put it in `.env` as `GOOGLE_SHEET_ID=...`
5. Test:

```bash
python test_sheets.py
```

---

## 5) Google Calendar setup

1. Open [Google Calendar](https://calendar.google.com)
2. Hover your calendar → **⋮** → **Settings and sharing**
3. **Share with specific people** → add the same `client_email`
4. Permission: **Make changes to events** → Send
5. Set in `.env`:

```env
GOOGLE_CALENDAR_ID=your_email@gmail.com
```

---

## 6) Discord setup (optional)

1. Discord → Server Settings → Integrations → Webhooks → New Webhook
2. Copy webhook URL into `DISCORD_WEBHOOK_URL` in `.env`

---

## 7) Run it

Full pipeline (Sheets + Calendar + Discord):

```bash
python main.py --all
```

Useful commands:

| Command | What it does |
|---|---|
| `python main.py --all` | Full sync |
| `python main.py --google` | Sheets only |
| `python main.py --google --calendar` | Sheets + Calendar |
| `python main.py --weekly` | Excel + Discord digest |
| `python main.py --google --skip-canvas-download` | Use local `syllabi/` PDFs only |
| `python main.py --course "MATH 201"` | Limit syllabus download to one course |

On each run you should see logs like:

```text
[INFO] Retrieved: X Canvas items, Y Syllabus items
[INFO] Discarded: Z duplicate entries
[INFO] Calendar: A created, B updated, C unchanged, D duplicates deleted
```

---

## How duplicates are prevented

- Each assignment gets a stable `Task_ID`
- Canvas wins over syllabus when both describe the same task
- Google Sheets upserts by key and preserves your Status/Priority edits
- Google Calendar matches by `task_id` + fuzzy title (`Assignment #1 ...`) and **deletes leftover Sylla Sync duplicates**
- Lecture/lab schedule events on your calendar are never touched

---

## Safety (do not skip)

Never commit these files:

- `.env`
- `credentials.json`
- syllabus PDFs in `syllabi/`

They are already listed in `.gitignore`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Missing env var | Fill real values in `.env` (not placeholder text) |
| Sheets permission error | Share sheet with service account email as Editor |
| Calendar 403 / Not Found | Enable Calendar API + share calendar as **Make changes to events** |
| Duplicate calendar events | Re-run `python main.py --google --calendar` (cleanup is automatic) |
| Gemini 429 quota | Wait or rely on built-in PDF parsers for structured syllabi |
| No PDFs found | Add files to `syllabi/` or remove `--skip-canvas-download` |
| ModuleNotFoundError | Activate venv, then `pip install -r requirements.txt` |

---

## Project layout

```text
SyllaSync/
├── main.py                   # CLI entry point (--all pipeline)
├── dedupe_module.py          # Task_ID + anti-duplicate engine
├── canvas_module.py          # Canvas assignments
├── canvas_syllabus_module.py # Download syllabi from Canvas
├── syllabus_module.py        # PDF parsing
├── google_sheets_module.py   # Google Sheets sync
├── google_calendar_module.py # Google Calendar sync
├── discord_module.py         # Discord digest
├── config.py                 # Loads .env
├── .env.example
└── credentials.json.example
```
