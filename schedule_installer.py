"""Windows Task Scheduler helpers and a simple cross-platform daily daemon.

Uses only the Python standard library. The install/uninstall path shells out to
``schtasks`` (Windows). On other platforms those entry points refuse to run and
point the user at ``--daemon`` instead.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger("syllasync.schedule")

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_TASK_NAME = "SyllaSync Daily"
DEFAULT_TIME = "18:00"
SCHTASKS_TR_MAX = 261
WRAPPER_CMD_NAME = "syllasync_daily.cmd"


def validate_time(value: str) -> str:
    """Return *value* if it is a valid 24-hour ``HH:MM`` string; else raise."""
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise ValueError(
            f"Invalid time {value!r}: expected HH:MM in 24-hour form "
            "(e.g. 09:30 or 18:00)."
        )
    return value


def _default_repo_dir() -> Path:
    return Path(__file__).resolve().parent


def _quote_win(path: str | Path) -> str:
    """Wrap a path in double quotes for cmd.exe (handles spaces)."""
    text = str(path)
    if text.startswith('"') and text.endswith('"'):
        return text
    return f'"{text}"'


def write_daily_wrapper(
    *,
    python_exe: str | Path,
    repo_dir: str | Path,
) -> Path:
    """Write a short gitignored .cmd that cds into the repo and runs --all.

    Used when the inline ``/TR`` string would exceed the schtasks 261-char limit.
    """
    repo = Path(str(repo_dir))
    python = str(python_exe)
    log_file = repo / "logs" / "daily_sync.log"
    wrapper = repo / WRAPPER_CMD_NAME
    lines = [
        "@echo off",
        f"cd /d {_quote_win(repo)}",
        "if not exist logs mkdir logs",
        f"{_quote_win(python)} main.py --all >> {_quote_win(log_file)} 2>&1",
        "",
    ]
    wrapper.write_text(chr(10).join(lines), encoding="utf-8", newline=chr(13)+chr(10))
    return wrapper


def build_tr_command(
    *,
    python_exe: str | Path,
    repo_dir: str | Path,
) -> str:
    """Build the ``/TR`` payload: cd into the repo, run ``main.py --all``, log.

    Paths are kept as plain strings (no host ``Path.resolve``) so a Windows
    ``schtasks`` command built on any OS still uses Windows path text. Log
    paths always use backslashes, matching cmd.exe on Windows.

    If the inline command exceeds ``SCHTASKS_TR_MAX`` (261), write
    ``syllasync_daily.cmd`` in the repo and point ``/TR`` at that wrapper.
    """
    repo = str(repo_dir).rstrip("\\/")
    python = str(python_exe)
    log_file = f"{repo}\\logs\\daily_sync.log"
    amp = "&&"
    inline = (
        "cmd /c "
        f"cd /d {_quote_win(repo)} "
        f"{amp} if not exist logs mkdir logs "
        f"{amp} {_quote_win(python)} main.py --all "
        f">> {_quote_win(log_file)} 2>&1"
    )
    if len(inline) <= SCHTASKS_TR_MAX:
        return inline
    wrapper = write_daily_wrapper(python_exe=python, repo_dir=repo)
    return str(wrapper)



def build_install_command(
    time: str = DEFAULT_TIME,
    task_name: str = DEFAULT_TASK_NAME,
    python_exe: str | None = None,
    repo_dir: str | Path | None = None,
) -> list[str]:
    """Build ``schtasks /Create ...`` argv for a daily SyllaSync run.

    The ``/TR`` value cds into *repo_dir*, runs ``main.py --all`` with
    *python_exe*, and appends stdout/stderr to ``logs\\daily_sync.log``.
    Paths with spaces are quoted for cmd.exe. Returns a list suitable for
    ``subprocess.run`` (no shell=True).
    """
    validate_time(time)
    if python_exe is None:
        python_exe = sys.executable
    if repo_dir is None:
        repo_dir = _default_repo_dir()

    tr = build_tr_command(python_exe=python_exe, repo_dir=repo_dir)
    return [
        "schtasks",
        "/Create",
        "/SC",
        "DAILY",
        "/ST",
        time,
        "/TN",
        task_name,
        "/TR",
        tr,
        "/F",
    ]


def build_uninstall_command(task_name: str = DEFAULT_TASK_NAME) -> list[str]:
    """Build ``schtasks /Delete /TN <name> /F`` argv."""
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def build_query_command(task_name: str = DEFAULT_TASK_NAME) -> list[str]:
    """Build ``schtasks /Query /TN <name> /V /FO LIST`` argv."""
    return ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"]


def _run_schtasks(argv: list[str]) -> int:
    """Run a schtasks argv list; print a short result; return the exit code."""
    logger.info("Running: %s", " ".join(argv))
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    return int(completed.returncode)


def install_schedule(
    time: str = DEFAULT_TIME,
    task_name: str = DEFAULT_TASK_NAME,
    python_exe: str | None = None,
    repo_dir: str | Path | None = None,
) -> int:
    """Register a daily Task Scheduler job. Windows only; returns the exit code."""
    if sys.platform != "win32":
        print(
            "Task Scheduler install is only available on Windows.\n"
            "On this system, keep the process alive instead with:\n"
            f"  python main.py --daemon --time {time}"
        )
        return 1

    try:
        argv = build_install_command(
            time=time,
            task_name=task_name,
            python_exe=python_exe,
            repo_dir=repo_dir,
        )
    except ValueError as exc:
        print(f"Cannot install schedule: {exc}", file=sys.stderr)
        return 2

    # Ensure logs/ exists up front so the first scheduled run can append.
    repo = Path(repo_dir) if repo_dir is not None else _default_repo_dir()
    (repo / "logs").mkdir(parents=True, exist_ok=True)

    code = _run_schtasks(argv)
    if code == 0:
        print(
            f"Installed daily task {task_name!r} at {time}. "
            "Open Task Scheduler to confirm, or run:\n"
            f"  schtasks /Query /TN \"{task_name}\" /V /FO LIST"
        )
    else:
        print(
            f"schtasks failed with exit code {code}. "
            "Try running the terminal as your normal user (not elevated) "
            "unless you intentionally set /RU options.",
            file=sys.stderr,
        )
    return code


def uninstall_schedule(task_name: str = DEFAULT_TASK_NAME) -> int:
    """Remove a Task Scheduler job. Windows only; returns the exit code."""
    if sys.platform != "win32":
        print(
            "Task Scheduler uninstall is only available on Windows.\n"
            "Nothing was changed on this system."
        )
        return 1

    argv = build_uninstall_command(task_name=task_name)
    code = _run_schtasks(argv)
    if code == 0:
        print(f"Removed scheduled task {task_name!r}.")
    else:
        print(
            f"schtasks delete failed with exit code {code}. "
            "The task may already be absent.",
            file=sys.stderr,
        )
    return code


def seconds_until_next(hhmm: str, now: datetime) -> float:
    """Seconds from *now* until the next local occurrence of ``HH:MM``."""
    validate_time(hhmm)
    hour, minute = (int(part) for part in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def run_daemon(
    time: str = DEFAULT_TIME,
    run_fn: Callable[[], None] | None = None,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    clock_fn: Callable[[], datetime] | None = None,
    stop_after: int | None = None,
) -> None:
    """Sleep until the next ``HH:MM`` local time, call *run_fn*, and repeat.

    *sleep_fn* and *clock_fn* are injectable for tests. *stop_after* limits how
    many successful wake-ups run (also for tests); ``None`` means forever.
    Exceptions from *run_fn* are logged and do not stop the loop.
    """
    validate_time(time)
    if run_fn is None:
        raise ValueError("run_fn is required")

    sleep = sleep_fn if sleep_fn is not None else time_module.sleep
    clock = clock_fn if clock_fn is not None else datetime.now

    runs = 0
    while True:
        now = clock()
        delay = seconds_until_next(time, now)
        logger.info("Daemon sleeping %.1f s until next run at %s", delay, time)
        sleep(delay)
        try:
            run_fn()
        except Exception:
            logger.exception("Scheduled daemon run failed")
        runs += 1
        if stop_after is not None and runs >= stop_after:
            break
