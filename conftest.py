"""Pytest configuration: makes the project root importable in all run modes."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.query_session import TaskDraftEngine


@pytest.fixture(autouse=True)
def _deterministic_draft_clock(monkeypatch):
    """Give the staged CREATE flow a fixed clock.

    The reminder tests use absolute dates such as ``15 November 2026 at 5 PM``.
    Without a frozen clock those tests pass or fail depending on the hour the
    suite runs. An engine constructed with an explicit ``now=`` keeps it.
    """

    fixed_now = datetime(2026, 11, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    original_now = TaskDraftEngine._now

    def _now(self):
        if self._fixed_now is not None:
            return original_now(self)

        return fixed_now

    monkeypatch.setattr(TaskDraftEngine, "_now", _now)
