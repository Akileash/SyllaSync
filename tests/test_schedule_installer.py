"""Tests for schedule_installer (mocked schtasks / platform / clock)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from syllasync.schedule_installer import (
    build_install_command,
    build_query_command,
    build_uninstall_command,
    install_schedule,
    run_daemon,
    seconds_until_next,
    uninstall_schedule,
    validate_time,
)


def test_validate_time_accepts_hhmm():
    assert validate_time("00:00") == "00:00"
    assert validate_time("18:00") == "18:00"
    assert validate_time("23:59") == "23:59"


@pytest.mark.parametrize(
    "bad",
    ["18", "18:0", "8:00", "24:00", "18:60", "25:01", "ab:cd", "", "18:00:00", " 18:00"],
)
def test_validate_time_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        validate_time(bad)


def test_build_install_command_argv_shape():
    argv = build_install_command(
        time="09:30",
        task_name="SyllaSync Daily",
        python_exe=r"C:\Tools\python.exe",
        repo_dir=r"C:\Repos\SyllaSync",
    )
    assert argv[0] == "schtasks"
    assert argv[1:5] == ["/Create", "/SC", "DAILY", "/ST"]
    assert argv[5] == "09:30"
    assert "/TN" in argv
    assert argv[argv.index("/TN") + 1] == "SyllaSync Daily"
    assert "/TR" in argv
    assert argv[-1] == "/F"
    tr = argv[argv.index("/TR") + 1]
    assert tr.startswith("cmd /c ")
    assert "main.py --all" in tr
    assert r"C:\Repos\SyllaSync" in tr
    assert "daily_sync.log" in tr


def test_build_install_command_quotes_paths_with_spaces():
    python = r"C:\Program Files\Python311\python.exe"
    repo = r"C:\Users\Ada Lovelace\Documents\My Projects\SyllaSync"
    argv = build_install_command(
        time="18:00",
        task_name="SyllaSync Daily",
        python_exe=python,
        repo_dir=repo,
    )
    tr = argv[argv.index("/TR") + 1]
    assert f'cd /d "{repo}"' in tr
    assert f'"{python}" main.py --all' in tr
    assert f'>> "{repo}\\logs\\daily_sync.log"' in tr or f'>> "{repo}/logs/daily_sync.log"' in tr
    # Outer argv list keeps /TR as a single element (subprocess-safe).
    assert argv.count("/TR") == 1


def test_build_install_command_rejects_bad_time():
    with pytest.raises(ValueError):
        build_install_command(time="99:99", python_exe="py", repo_dir="C:\\repo")


def test_build_uninstall_command_argv():
    assert build_uninstall_command("SyllaSync Daily") == [
        "schtasks",
        "/Delete",
        "/TN",
        "SyllaSync Daily",
        "/F",
    ]


def test_build_query_command_argv():
    argv = build_query_command("SyllaSync Daily")
    assert argv[0] == "schtasks"
    assert "/Query" in argv
    assert argv[argv.index("/TN") + 1] == "SyllaSync Daily"


def test_install_schedule_non_windows_guard(capsys):
    with patch("syllasync.schedule_installer.sys.platform", "linux"):
        code = install_schedule(time="18:00")
    assert code == 1
    captured = capsys.readouterr()
    assert "Windows" in captured.out
    assert "--daemon" in captured.out


def test_uninstall_schedule_non_windows_guard(capsys):
    with patch("syllasync.schedule_installer.sys.platform", "linux"):
        code = uninstall_schedule()
    assert code == 1
    assert "Windows" in capsys.readouterr().out


def test_install_schedule_runs_subprocess_on_windows(tmp_path):
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "SUCCESS: The scheduled task was created."
    fake.stderr = ""
    with (
        patch("syllasync.schedule_installer.sys.platform", "win32"),
        patch("syllasync.schedule_installer.subprocess.run", return_value=fake) as run,
    ):
        code = install_schedule(
            time="18:00",
            task_name="SyllaSync Daily",
            python_exe=str(tmp_path / "python.exe"),
            repo_dir=tmp_path,
        )
    assert code == 0
    run.assert_called_once()
    kwargs = run.call_args.kwargs
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"
    argv = run.call_args.args[0]
    assert argv[0] == "schtasks"
    assert "/Create" in argv
    assert (tmp_path / "logs").is_dir()


def test_uninstall_schedule_runs_subprocess_on_windows():
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "SUCCESS"
    fake.stderr = ""
    with (
        patch("syllasync.schedule_installer.sys.platform", "win32"),
        patch("syllasync.schedule_installer.subprocess.run", return_value=fake) as run,
    ):
        code = uninstall_schedule(task_name="SyllaSync Daily")
    assert code == 0
    argv = run.call_args.args[0]
    assert argv == ["schtasks", "/Delete", "/TN", "SyllaSync Daily", "/F"]


def test_seconds_until_next_same_day():
    now = datetime(2026, 3, 15, 17, 0, 0)
    assert seconds_until_next("18:00", now) == 3600.0


def test_seconds_until_next_rolls_to_tomorrow():
    now = datetime(2026, 3, 15, 18, 0, 0)
    assert seconds_until_next("18:00", now) == 24 * 3600.0


def test_run_daemon_calls_run_fn_once_with_fake_clock_and_sleep():
    sleeps: list[float] = []
    runs: list[str] = []

    def fake_clock() -> datetime:
        return datetime(2026, 6, 1, 17, 30, 0)

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_run() -> None:
        runs.append("tick")

    run_daemon(
        time="18:00",
        run_fn=fake_run,
        sleep_fn=fake_sleep,
        clock_fn=fake_clock,
        stop_after=1,
    )

    assert runs == ["tick"]
    assert len(sleeps) == 1
    assert sleeps[0] == 30 * 60.0


def test_run_daemon_logs_exception_and_continues():
    calls = {"n": 0}

    def boom_then_ok() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    run_daemon(
        time="18:00",
        run_fn=boom_then_ok,
        sleep_fn=lambda _s: None,
        clock_fn=lambda: datetime(2026, 1, 1, 17, 0, 0),
        stop_after=2,
    )
    assert calls["n"] == 2


def test_run_daemon_requires_run_fn():
    with pytest.raises(ValueError):
        run_daemon(time="18:00", run_fn=None)


def test_build_tr_uses_wrapper_when_over_261(tmp_path):
    from syllasync.schedule_installer import SCHTASKS_TR_MAX, WRAPPER_CMD_NAME, build_tr_command

    python = tmp_path / ("python" + ("x" * 80) + ".exe")
    repo = tmp_path / ("repo" + ("y" * 80))
    repo.mkdir()
    # Invented long paths; no need for the exe to exist for command building.
    inline_len = len(
        "cmd /c "
        f'cd /d "{repo}" '
        "&& if not exist logs mkdir logs "
        f'&& "{python}" main.py --all '
        f'>> "{repo}\\logs\\daily_sync.log" 2>&1'
    )
    assert inline_len > SCHTASKS_TR_MAX
    tr = build_tr_command(python_exe=python, repo_dir=repo)
    assert tr.endswith(WRAPPER_CMD_NAME)
    assert len(tr) <= SCHTASKS_TR_MAX or Path(tr).name == WRAPPER_CMD_NAME
    wrapper = repo / WRAPPER_CMD_NAME
    assert wrapper.is_file()
    body = wrapper.read_text(encoding="utf-8")
    assert "main.py --all" in body
    assert "daily_sync.log" in body

