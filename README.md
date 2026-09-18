# Sylla Sync

Sync university assignments from **Canvas** + **syllabus / lab schedule PDFs** into Google Sheets, Google Calendar, Discord, and/or a local Excel dashboard.

---

## AI agent prompt (copy everything below this line)

```text
You are setting up and operating Sylla Sync for me. Treat this README as the
source of truth. Do the work yourself with terminal tools; ask me only when a
secret, click-through OAuth/share step, or personal timetable detail is required.

============================================================
MISSION
============================================================
Configure Sylla Sync so my Canvas + syllabus assignments reliably appear in:
  1) Google Sheets Masterlist (HHS Assignment Tracker template)
  2) Google Calendar (Sylla Sync–managed events only)
  3) Optional Discord weekly digest
Never commit secrets. Never invent due dates. Prefer --dry-run before live writes.

============================================================
HARD RULES (never violate)
============================================================
1. NEVER commit, paste into chat logs, or push:
   - .env / local.env
   - credentials.json
   - schedule.json
   - .syllasync_task_state.json
   - anything under syllabi/
   - token / webhook / API key values
2. NEVER force-push, amend others’ commits, or skip git hooks unless I ask.
3. NEVER delete or edit my non–Sylla Sync calendar events (lectures/labs from
   university ICS). Only touch events Sylla Sync created (description contains
   "Sylla Sync" or private extendedProperties syllasync=1 / task_id).
4. NEVER invent Canvas due dates. If Canvas due_at is null, the row may exist
   on Sheets with a blank due date; Calendar MUST skip it until a real date exists.
5. Calendar sync must:
   - ignore cancelled/trashed Google Calendar events
   - recreate (insert) when an update returns HTTP 410
   - never match short titles via prefix (HW1 must not match HW10)
   - set event status to "confirmed" on create/update
6. Preserve user Status edits on Sheets when merging (Submitted / Complete stay).
7. Use the project venv if present; otherwise create one and pip install -r
   requirements.txt.
8. Prefer absolute paths for this repo when running commands.

============================================================
REPO LAYOUT (what matters)
============================================================
main.py                  orchestrator (--google --calendar --weekly --all …)
config.py                loads .env / local.env
canvas_module.py         live Canvas assignments
canvas_syllabus_module.py download syllabus/schedule PDFs → syllabi/
syllabus_module.py       parse local PDFs (Gemini optional)
dedupe_module.py         Task_ID + cross-source dedupe
google_sheets_module.py  HHS Masterlist write (cols A–H; leave I+)
google_calendar_module.py Calendar upsert + obsolete cleanup
discord_module.py        weekly digest
schedule_module.py       schedule.json section filter
sheets_module.py         local Excel fallback
date_utils.py / vocab.py / course_utils.py / retry_utils.py
schedule.example.json    template → copy to schedule.json
.env.example             template → copy to .env
credentials.json         Google service account (user provides)
tests/                   run: python -m pytest tests/ -q

============================================================
SETUP SEQUENCE (do in order; stop and ask me if blocked)
============================================================

### Phase 0 — Clone / install
1. Ensure we are in the Sylla Sync repo root.
2. Create/activate venv and install deps:
   Windows:  python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt
   Unix:     python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
3. If missing:
   copy .env.example → .env
   copy schedule.example.json → schedule.json

### Phase 1 — Secrets (.env)
Fill .env with REAL values (ask me for anything missing):

CANVAS_URL=https://<school>.instructure.com
CANVAS_TOKEN=<Canvas Account → Settings → New Access Token>
ALLOWED_COURSES=MATH 201,MATH 209,MAT E 201,ECE 202,ECE 210,ENGG 299
LOCAL_TIMEZONE=America/Edmonton
GOOGLE_SHEET_ID=<id from the Sheet URL>
GOOGLE_CALENDAR_ID=<my Gmail OR Calendar Settings → Integrate calendar ID>
DISCORD_WEBHOOK_URL=<optional>
GEMINI_API_KEY=<optional; PDFs still parse without it>
# GEMINI_MODEL=gemini-2.5-flash

Notes:
- ALLOWED_COURSES blank = all active Canvas courses (usually bad).
- GOOGLE_CALENDAR_ID must be an email/calendar ID, NEVER an iCal/.ics URL.

### Phase 2 — Google Cloud + sharing (I must click; guide me)
1. Google Cloud Console → project → enable Sheets API + Calendar API.
2. Create service account → JSON key → save as credentials.json in repo root.
3. Read client_email from credentials.json (do not print private_key).
4. Sheets template credit: HHS Student Life @ Purdue
   Folder: https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK
   Or copy: https://docs.google.com/spreadsheets/d/1ALoho_3oHCHn7qsL3HuwOTkCWu2Rz3ZojE3SVflN65c/copy
5. I make a copy of the tracker → share with client_email as Editor.
6. Put Sheet ID into GOOGLE_SHEET_ID.
7. Google Calendar → Settings → Share with specific people → client_email
   permission: Make changes to events.
8. Smoke test Sheets: python test_sheets.py

### Phase 3 — schedule.json (section filter)
Ask me for my timetable. For EACH course record:
  course, type (LEC|LAB|SEM), section (EB1, EL10, …), days, start, end, location.
Write schedule.json (gitignored). Purpose: keep MY lab/lecture sections only
(e.g. MATH 209 LAB EL05, not every lab section on Canvas).

### Phase 4 — Dry-run then live
1. python main.py --all --dry-run
   Expect: PDF downloads into syllabi/, merge stats, no external mutations.
2. Show me the dry-run summary. If I approve:
   python main.py --all
3. Useful variants:
   python main.py --google --calendar          # Sheets + Calendar cleanup
   python main.py --course "MATH 201" --google # one course
   python main.py --skip-canvas-download …     # reuse syllabi/
   python main.py --allow-partial              # continue if Canvas fails
   python main.py --syllabus-only              # local PDFs only

### Phase 5 — Verify
Sheets Masterlist: rows for my courses with Task_ID in column A.
Calendar: events titled like "MATH 201 - HW2" or "[SUBMITTED] MATH 201 - HW1".
Discord (if configured): digest skips Completed/Submitted items.
Run: python -m pytest tests/ -q

============================================================
BEHAVIORAL CONTRACT (how the pipeline works)
============================================================
Sources → dedupe → Sheets and/or Excel → optional Calendar → optional Discord.

Task_ID:
  Canvas:   canvas_{assignment_id}
  Syllabus: syllabus_{course}_{title}_{due}
Canvas wins over syllabus when fuzzy course+title match.
Aliases: MTH 201 / MATH 201W → MATH 201.

Sheets (Masterlist starting row 11):
  A Task_ID | B Status | C Due Date | D Due Date (calc) | E Time
  F Class | G Type | H Assignment
  Columns I+ are template formulas — NEVER overwrite.
  Titles must not keep embedded "Due date …" text; move into C/D/E.
  Drop bare placeholders: "Labs", "Assignments", etc.

Calendar:
  Skip: blank/TBD/unparseable due, draft rows, cancelled status, placeholders.
  Timed dues → timed event ending at due time (default 60 min span).
  Date-only → all-day event.
  Upsert by task_id / fuzzy_key; delete obsolete Sylla Sync events.
  If event was trashed (410), INSERT a new confirmed event — do not leave gaps.
  Short homework codes (HW1 vs HW10) match by fingerprint only, not prefix.

Discord:
  Upcoming window only; skip finished statuses (Complete/Submitted/…).

============================================================
KNOWN FAILURE MODES → FIX
============================================================
| Symptom | Cause | Agent action |
|---|---|---|
| On Sheets, missing on Calendar, blank due | Canvas due_at null | Tell me; do not invent date. Re-sync after Canvas/I set a date. |
| On Sheets with date, missing on Calendar | Trashed event / 410 / bad match | Run python main.py --google --calendar. Code must recreate on 410. |
| HW1 collided with HW10 / wrong event updated | Prefix match bug | Ensure tests/test_hardening.py calendar HW1≠HW10 tests pass. |
| Calendar 403 | API off or not shared | Enable Calendar API; share Make changes to events. |
| Sheets permission error | Not shared Editor | Share client_email as Editor. |
| Wrong lab section synced | schedule.json wrong | Fix section codes; re-run. |
| Duplicate Labs / −3 day phantoms | Category placeholders | Re-run --google --calendar; phantoms should drop. |
| ModuleNotFoundError | Wrong interpreter | Activate venv; pip install -r requirements.txt. |
| Gemini 404 | Bad model name | Use supported flash model or omit GEMINI_API_KEY. |
| iCal URL in GOOGLE_CALENDAR_ID | Misconfigured | Replace with email / calendar ID. |

============================================================
WHEN I REPORT “ON SHEET BUT NOT CALENDAR”
============================================================
Diagnose before guessing:
1. Load Masterlist row(s) for that course/title (raw due + status + Task_ID).
2. Check whether Due Date parses; if empty → explain Canvas/sheet gap.
3. List Sylla Sync calendar events (include showDeleted if needed).
4. If status=cancelled for that task_id → recreate via calendar sync (fixed path).
5. Re-run python main.py --google --calendar and confirm event is confirmed.
6. Do not claim “no due date” if columns C/D show a date — re-read raw cells.

============================================================
DONE CRITERIA
============================================================
- [ ] venv + requirements installed
- [ ] .env filled (no placeholders)
- [ ] credentials.json present; Sheet + Calendar shared with service account
- [ ] schedule.json has my lecture/lab section numbers
- [ ] pytest tests/ passes
- [ ] dry-run then live --all succeeded
- [ ] git status clean of secrets; secrets remain gitignored
```

---

## Human quick start (same flow, shorter)

1. `pip install -r requirements.txt` (in a venv)
2. Copy `.env.example` → `.env` and `schedule.example.json` → `schedule.json`
3. Add Canvas token + Google IDs; drop `credentials.json` in the repo root
4. Share Sheet (Editor) + Calendar (**Make changes to events**) with the service account email
5. Fill `schedule.json` with **your** LEC/LAB section codes
6. `python main.py --all --dry-run` then `python main.py --all`

| Command | Purpose |
|---|---|
| `python main.py --all` | Sheets + Calendar + Discord |
| `python main.py --google --calendar` | Sheets + Calendar cleanup |
| `python main.py --course "MATH 201" --google` | One course |
| `python main.py --skip-canvas-download --google` | Reuse `syllabi/` |
| `python -m pytest tests/ -q` | Regression tests |

---

## Credits

Google Sheets assignment tracker template by **HHS Student Life @ Purdue**:  
https://drive.google.com/drive/folders/16OVKLNUKoe5slXzq4g3rC4kSgVLTWgYK
