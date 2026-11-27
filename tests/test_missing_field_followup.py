from types import SimpleNamespace
from datetime import date, time

import services.missing_field_followup as followup


def test_detect_missing_deadline_fields():
    task = SimpleNamespace(task_description="x", deadline_date=None, deadline_time=None, progress=0)
    assert followup.detect_missing_fields(task) == ["deadline_date", "deadline_time"]


def test_detect_missing_partial_deadline_time():
    task = SimpleNamespace(task_description="x", deadline_date=date(2026, 11, 28), deadline_time=None, progress=0)
    assert followup.detect_missing_fields(task) == ["deadline_time"]


def test_no_missing_when_defaults_present():
    task = SimpleNamespace(task_description="desc", deadline_date=date(2026, 11, 28), deadline_time=time(17, 0), progress=0)
    assert followup.detect_missing_fields(task) == []
