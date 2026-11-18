# SyllaSync

I got tired of tracking deadlines across Canvas and a pile of syllabi, so I wrote this to do it for me. SyllaSync pulls the assignments and PDFs onto a Google Sheet, puts the due dates on Calendar, and can post what's coming up to Discord. Way quicker than checking everything by hand. The name is just syllabus plus sync, nothing fancy.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Setup

Python 3.10+.

**Windows**

```powershell
py -3 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item examples\schedule.example.json schedule.json
```

**macOS / Linux**

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp examples/schedule.example.json schedule.json
```

Put Google credentials in `credentials.json` (gitignored). Two options:

- Service account: download a JSON key (`examples/credentials.json.example`). Share the Sheet (Editor) and Calendar (Make changes to events) with `client_email`.
- Installed-app OAuth: download a Desktop client secrets file (`examples/credentials.oauth.json.example`). The first Sheets or Calendar run opens a browser and stores a refresh token in `token.json` (also gitignored). If the OAuth client is in testing mode, add your Google account as a test user. You do not share the Sheet or Calendar with a service account.

Tab name: `Masterlist`. Notes: `docs/google-sheet-instructions/`.

Then run the setup wizard (writes `.env`, does not print secrets, does not change `credentials.json`):

```
python main.py --setup
```

Or copy `examples/.env.example` to `.env` and edit it (UTF-8). Never commit real values.

Required for `--all`: `CANVAS_URL`, `CANVAS_TOKEN`, `GOOGLE_SHEET_ID`, `GOOGLE_CALENDAR_ID`, `DISCORD_WEBHOOK_URL`

Optional: `GEMINI_API_KEY`, `GEMINI_MODEL`, `ALLOWED_COURSES`, `LOCAL_TIMEZONE`, `TERM_YEAR`, `CANVAS_PAST_DUE_DAYS`, `EXCEL_FILE_PATH`, `SYNC_GRADES`, `STALE_YEAR_POLICY`

`local.env` overrides `.env`. Fill sections in `schedule.json`.

```
python main.py --check --all
```

Exits 1 on missing or placeholder values. No network calls, no secrets printed. `python main.py --check` only needs Canvas.

## Running

```powershell
python main.py --all --dry-run
python main.py --all
```

`--all` writes Sheet, Calendar, and Discord. `--dry-run` previews creates/updates/deletes and writes nothing.

## Privacy

`--all` sends assignment titles and due dates to your Google Sheet and Calendar. Canvas grades go in Sheet columns K-M unless you set `SYNC_GRADES=0` (default is on, so nothing changes if you leave it). If `DISCORD_WEBHOOK_URL` is set, the weekly digest posts upcoming course, title, due date, and priority to that webhook. If `GEMINI_API_KEY` is set, extracted syllabus text is sent to Gemini; leave it unset to parse locally.

Secrets stay in `.env`, `local.env`, `credentials.json`, and `token.json` on this machine. Those files are gitignored. `--check` and the logs print variable names only, never the values. You can set the same names as environment variables instead of putting them in a file (`.env` will not overwrite ones already set).

## Optional local JSON

Gitignored. Copy from `examples\` and edit.

### `due_overrides.json`

Pin a due time when Canvas or a syllabus is wrong. Match by course and title. `end` is optional. Canvas dues in a year before `TERM_YEAR` stay as-is and are warned (`STALE_YEAR_POLICY=warn`, the default). `override` still leaves them as-is and prints a one-line pin hint. `shift` coerces those dates onto the term year the same way syllabus dates are, unless a pin already exists.

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

Daily `main.py --all` at 18:00, logged to `logs/daily_sync.log`.

### Windows

Task Scheduler is the recommended option (task name `SyllaSync Daily`):

```powershell
python main.py --install-schedule --time 18:00
python main.py --uninstall-schedule
```

Use `--task-name` to change the task name.

### macOS and Linux

`python main.py --install-schedule --time 18:00` prints a cron line, plus a launchd plist (macOS) or a systemd user timer (Linux). It does not install anything. Example cron (edit the paths, then `crontab -e`):

```
0 18 * * * cd /path/to/SyllaSync && mkdir -p logs && ./venv/bin/python main.py --all >> logs/daily_sync.log 2>&1
```

On macOS, save the printed plist under `~/Library/LaunchAgents/` and `launchctl load` it. On Linux, save the printed unit and timer under `~/.config/systemd/user/` and run `systemctl --user enable --now syllasync.timer`.

Or leave a process running: `python main.py --daemon --time 18:00`.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Akileash Saravanan.
