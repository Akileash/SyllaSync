# SyllaSync

I got tired of tracking deadlines across Canvas and a pile of syllabi, so I wrote this to do it for me. SyllaSync pulls the assignments and PDFs onto a Google Sheet, puts the due dates on Calendar, and can post what's coming up to Discord. Way quicker than checking everything by hand. The name is just syllabus plus sync, nothing fancy.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Windows setup

```powershell
py -3 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item examples\.env.example .env
Copy-Item examples\schedule.example.json schedule.json
```

Edit `.env` (UTF-8). Never commit real values.

Required for `--all`: `CANVAS_URL`, `CANVAS_TOKEN`, `GOOGLE_SHEET_ID`, `GOOGLE_CALENDAR_ID`, `DISCORD_WEBHOOK_URL`

Optional: `GEMINI_API_KEY`, `GEMINI_MODEL`, `ALLOWED_COURSES`, `LOCAL_TIMEZONE`, `TERM_YEAR`, `CANVAS_PAST_DUE_DAYS`, `EXCEL_FILE_PATH`

`local.env` overrides `.env`. Fill sections in `schedule.json`.

Put a Google service-account key in `credentials.json` (shape: `examples\credentials.json.example`). Share the Sheet (Editor) and Calendar (Make changes to events) with `client_email`. Tab name: `Masterlist`. Notes: `docs\google-sheet-instructions\`.

```powershell
python main.py --check --all
```

Exits 1 on missing or placeholder values. No network calls, no secrets printed. `python main.py --check` only needs Canvas.

## Running

```powershell
python main.py --all --dry-run
python main.py --all
```

`--all` writes Sheet, Calendar, and Discord. `--dry-run` previews creates/updates/deletes and writes nothing.

## Optional local JSON

Gitignored. Copy from `examples\` and edit.

### `due_overrides.json`

Pin a due time when Canvas or a syllabus is wrong. Match by course and title. `end` is optional:

```json
{
  "overrides": [
    {
      "course": "PHYS 100",
      "title": "Midterm Review Quiz",
      "due": "2026-12-01 17:00",
      "end": "2026-12-01 18:00"
    },
    {
      "course": "CHEM 101",
      "title": "Lab Report 2",
      "due": "2026-11-15 23:59"
    }
  ]
}
```

### `ignored_items.json`

Hide items by `task_id`:

```json
{
  "ignored": [
    { "task_id": "canvas_1001" },
    { "task_id": "syllabus_phys_100_attendance_check_in" }
  ]
}
```

## Scheduling

Daily Task Scheduler job for `main.py --all` (default 18:00, name `SyllaSync Daily`). Logs to `logs\daily_sync.log`.

```powershell
python main.py --install-schedule --time 18:00
python main.py --uninstall-schedule
```

Windows only. Use `--task-name` to change the task name.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Akileash Saravanan.
