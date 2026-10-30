"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from syllasync import dedupe_module


@pytest.fixture(autouse=True)
def _isolated_task_state(tmp_path, monkeypatch):
    """Keep tests away from the real .syllasync_task_state.json in the project folder."""
    monkeypatch.setattr(dedupe_module, "STATE_PATH", tmp_path / ".syllasync_task_state.json")
