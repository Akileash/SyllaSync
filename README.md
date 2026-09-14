# Sylla Sync

Sync university assignments from **Canvas** + **syllabus / lab schedule PDFs** into Google Sheets, Google Calendar, Discord, and/or a local Excel dashboard.

**Follow these steps in order.**

---

## Step 1 — Install

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

Copy config templates:

```bash
# Windows
copy .env.example .env
copy schedule.example.json schedule.json

# macOS / Linux
cp .env.example .env
cp schedule.example.json schedule.json
```

You will also add `credentials.json` in Step 2 (Google).

---

## Step 2 — API setup

### A. Canvas (required)

1. Canvas → **Account → Settings → New Access Token**
2. Put these in `.env`:

```env
CANVAS_URL=https://your-university.instructure.com
CANVAS_TOKEN=your_canvas_api_token
ALLOWED_COURSES=MATH 201,MATH 209,MAT E 201,ECE 202,ECE 210,ENGG 299
LOCAL_TIMEZONE=America/Edmonton
```

Leave `ALLOWED_COURSES` blank only if you want every active Canvas course.

### B. Google Sheets + Calendar (for `--google` / `--calendar` / `--all`)

1. [Google Cloud Console](https://console.cloud.google.com/) → create/select a project  
2. Enable:
   - [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
   - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
3. Create a **service account** → Keys → Add key → JSON  
4. Save that file as `credentials.json` in the Sylla Sync folder  
5. Copy `client_email` from inside `credentials.json`

**Sheets**

Google Sheet template credit: **HHS Student Life @ Purdue** —  
[template folder on Google Drive](https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK)

1. From that folder (or make a copy of the [HHS Assignment Tracker](https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy)), open the tracker and **File → Make a copy**
2. Share your copy with `client_email` as **Editor**
3. Put the Sheet ID in `.env` as `GOOGLE_SHEET_ID=...`
4. Test: `python test_sheets.py`

**Calendar**

1. Calendar settings → **Share with specific people** → add `client_email`
2. Permission: **Make changes to events**
3. Set `GOOGLE_CALENDAR_ID=your_email@gmail.com`

### C. Discord (optional, for `--weekly` / `--all`)

Server Settings → Integrations → Webhooks → New Webhook → paste URL into `DISCORD_WEBHOOK_URL`.

### D. Gemini (optional)

Syllabus PDFs still parse without it. If you want Gemini:

```env
# GEMINI_API_KEY=your_key
# GEMINI_MODEL=gemini-2.5-flash
```

---

## Step 3 — Put your schedule in + find your lab numbers

Sylla Sync uses `schedule.json` so it only keeps **your** lecture/lab/seminar sections (e.g. MATH 209 LAB **EL05**, not every lab).

1. Open your university timetable / Bear Tracks / registration page
2. For each course, write down:
   - course code (`MATH 201`)
   - type (`LEC`, `LAB`, `SEM`)
   - **section / lab number** (`EB1`, `EL10`, `D26`, …)
3. Edit `schedule.json` (created in Step 1):

```json
{
  "term": "Fall 2026",
  "institution": "University of Alberta",
  "timezone": "America/Edmonton",
  "sections": [
    {
      "course": "MATH 201",
      "type": "LEC",
      "section": "EB1",
      "days": ["Mon", "Wed", "Fri"],
      "start": "12:00",
      "end": "13:00",
      "location": "T B-95"
    },
    {
      "course": "MATH 201",
      "type": "LAB",
      "section": "EL10",
      "days": ["Mon"],
      "start": "16:00",
      "end": "17:00",
      "location": "CAB 377"
    }
  ]
}
```

`schedule.json` is gitignored — keep your personal timetable private.

---

## Step 4 — Pull syllabus / lab schedule PDFs from Canvas

You do **not** need a separate script for this. When you run `main.py`, Sylla Sync will:

1. Connect to Canvas with your token  
2. Respect `ALLOWED_COURSES` + your `schedule.json` lab/lecture sections  
3. Download syllabus / weekly schedule PDFs from:
   - Course **Modules**
   - Course **Files**
   - The Canvas **Syllabus** page  
4. Save them under `syllabi/`
5. Parse those PDFs for due dates (plus live Canvas assignments)

To only refresh PDFs for one course:

```bash
python main.py --course "MATH 201" --google --dry-run
```

To skip PDF download and reuse what is already in `syllabi/`:

```bash
python main.py --skip-canvas-download --google
```

---

## Step 5 — Run `main.py`

Preview (recommended first time):

```bash
python main.py --all --dry-run
```

Full sync (Sheets + Calendar + Discord):

```bash
python main.py --all
```

Other useful runs:

| Command | What it does |
|---|---|
| `python main.py --google` | Sheets only |
| `python main.py --google --calendar` | Sheets + Calendar (good cleanup run) |
| `python main.py --weekly` | Local Excel + Discord digest |
| `python main.py --allow-partial` | Continue if Canvas is down |
| `python main.py --syllabus-only` | Local PDFs only (no Canvas assignments) |

On a good run you should see logs like:

```text
[INFO] Retrieved: X Canvas items, Y Syllabus items
[INFO] Discarded: Z duplicate entries
[INFO] Calendar: A created, B updated, C unchanged, D deleted
```

---

## Checklist (do once)

1. [ ] `pip install -r requirements.txt`
2. [ ] `.env` filled (Canvas + Google IDs)
3. [ ] `credentials.json` present and sheet/calendar shared with service account
4. [ ] `schedule.json` has your **lab / lecture section numbers**
5. [ ] `python main.py --all --dry-run`
6. [ ] `python main.py --all`

---

## How it stays clean

- Stable `Task_ID`s across Sheets + Calendar  
- Canvas wins over syllabus duplicates  
- Course aliases collapse (`MTH 201` / `MATH 201W` → `MATH 201`)  
- Bare placeholders like “Labs” / “Assignments” are dropped  
- “Due date …” text is moved out of titles into date columns  
- Obsolete Sylla Sync calendar events are deleted  
- Your real lecture/lab calendar events are never touched  

---

## Safety

Never commit:

- `.env`
- `credentials.json`
- `schedule.json`
- `.syllasync_task_state.json`
- PDFs in `syllabi/`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Missing env var | Real values in `.env`, not placeholders |
| Sheets permission error | Share sheet with service account as **Editor** |
| Calendar 403 | Enable Calendar API + share as **Make changes to events** |
| Wrong lab synced | Fix `section` in `schedule.json`, re-run |
| Duplicates / “Labs −3 days” | `python main.py --google --calendar` |
| No PDFs downloaded | Check Canvas token; remove `--skip-canvas-download` |
| ModuleNotFoundError | Activate venv, then `pip install -r requirements.txt` |

---

## Ask an AI to set it up

```text
Set up Sylla Sync using this README, in this order:
1. Install venv + requirements.txt
2. Help me fill .env and create Google credentials.json (Sheets + Calendar APIs)
3. Help me build schedule.json from my timetable — include lab/lecture section numbers
4. Run python main.py --all --dry-run (this pulls syllabus/lab schedule PDFs from Canvas)
5. If that looks good, run python main.py --all
6. Confirm .env, credentials.json, and schedule.json are never committed
```

---

## Credits

Google Sheets assignment tracker template by **HHS Student Life @ Purdue**:  
https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK
