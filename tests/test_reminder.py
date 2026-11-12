"""Tests for ai/reminder.py: absolute and relative reminder parsing.

All tests inject a frozen reference clock so results never depend on the
real date: NOW = Thu 2026-11-12 10:00 Asia/Kolkata.
Deadline used everywhere: Sun 2026-11-15 17:00.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ai.reminder import ReminderError, parse_reminder

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
DEADLINE_DATE = date(2026, 11, 15)
DEADLINE_TIME = time(17, 0)


def parse(text, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("deadline_date", DEADLINE_DATE)
    kwargs.setdefault("deadline_time", DEADLINE_TIME)
    return parse_reminder(text, **kwargs)


# --- relative to the deadline --------------------------------------------------

def test_minutes_before():
    assert parse("30 minutes before the deadline") == (
        date(2026, 11, 15), time(16, 30)
    )


def test_hours_before():
    assert parse("2 hours before the deadline") == (
        date(2026, 11, 15), time(15, 0)
    )


def test_day_before():
    assert parse("1 day before the deadline") == (
        date(2026, 11, 14), time(17, 0)
    )


def test_three_days_before():
    assert parse("3 days before the deadline") == (
        date(2026, 11, 12), time(17, 0)
    )


def test_week_before_that_is_already_past_is_rejected():
    # a week before the Nov 15 deadline is Nov 8, before NOW (Nov 12)
    with pytest.raises(ReminderError) as error:
        parse("a week before the deadline")

    assert "past" in str(error.value).lower()


def test_half_an_hour_before():
    assert parse("half an hour before the deadline") == (
        date(2026, 11, 15), time(16, 30)
    )


def test_at_the_deadline():
    assert parse("at the deadline") == (DEADLINE_DATE, DEADLINE_TIME)


# --- absolute ------------------------------------------------------------------

def test_absolute_full_datetime():
    assert parse("15 November 2026 at 4 PM") == (
        date(2026, 11, 15), time(16, 0)
    )


def test_absolute_vague_date_and_time():
    assert parse("tomorrow at 9 am") == (date(2026, 11, 13), time(9, 0))


def test_absolute_date_only_needs_time():
    with pytest.raises(ReminderError) as error:
        parse("16 November 2026")

    assert "time" in str(error.value).lower()
    assert error.value.resolved_date == date(2026, 11, 16)


def test_context_completes_a_time_only_answer():
    result = parse(
        "5 pm",
        context={"date": "2026-11-14"},
    )
    assert result == (date(2026, 11, 14), time(17, 0))


# --- validation ----------------------------------------------------------------

def test_past_reminder_rejected():
    with pytest.raises(ReminderError) as error:
        parse("yesterday at 5 pm")

    assert "past" in str(error.value).lower()


def test_after_deadline_rejected():
    with pytest.raises(ReminderError) as error:
        parse("18 November 2026 at 5 PM")

    assert "after the deadline" in str(error.value).lower()


def test_garbage_rejected():
    with pytest.raises(ReminderError):
        parse("sometime whenever")


def test_empty_rejected():
    with pytest.raises(ReminderError):
        parse("   ")


def test_deadline_required():
    with pytest.raises(ReminderError):
        parse_reminder(
            "tomorrow at 9",
            deadline_date=None,
            deadline_time=None,
            now=NOW,
        )
