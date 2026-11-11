"""Tests for the date/time resolver.

All tests inject a frozen reference clock (now=...) so results never depend
on the real date. Reference points used:

    NOW   Wed 2026-11-11 09:00 Asia/Kolkata   (main clock)
    NOW2  Mon 2026-11-30 10:00                (month boundary)
    NOW3  Thu 2026-12-31 23:00                (year boundary)
    NOW4  Fri 2026-11-13 09:00                (weekday == today)
"""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from datetime_resolver import (
    DateResolver,
    ResolutionStatus,
    resolve,
)

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=TZ)       # Wednesday
NOW2 = datetime(2026, 11, 30, 10, 0, tzinfo=TZ)     # Monday, month boundary
NOW3 = datetime(2026, 12, 31, 23, 0, tzinfo=TZ)     # Thursday, year boundary
NOW4 = datetime(2026, 11, 13, 9, 0, tzinfo=TZ)      # Friday == reference weekday


def r(text: str, **kwargs):
    kwargs.setdefault("now", NOW)
    return resolve(text, **kwargs)


# --- relative words -----------------------------------------------------------

def test_today():
    result = r("Assign this task to me today")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION   # date-only -> time asked
    assert result.resolved_date == date(2026, 11, 11)
    assert result.weekday == "Wednesday"
    assert result.clarification.missing == ["time"]
    assert result.resolved_date is not None                        # date preserved, not discarded


def test_tomorrow():
    result = r("Remind me tomorrow at 10 AM")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.resolved_date == date(2026, 11, 12)
    assert result.resolved_time.isoformat() == "10:00:00"
    assert result.iso_datetime == "2026-11-12T10:00:00+05:30"


def test_yesterday():
    result = r("Yesterday I completed the task")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION   # date kept, time asked
    assert result.resolved_date == date(2026, 11, 10)
    assert result.resolved_time is None
    assert result.clarification.missing == ["time"]


def test_day_after_tomorrow():
    assert r("Complete this task day after tomorrow").resolved_date == date(2026, 11, 13)


def test_day_before_yesterday():
    assert r("The task was due day before yesterday").resolved_date == date(2026, 11, 9)


# --- boundaries ----------------------------------------------------------------

def test_month_rollover():
    tomorrow = resolve("Remind me tomorrow", now=NOW2)
    assert tomorrow.resolved_date == date(2026, 12, 1)
    assert tomorrow.weekday == "Tuesday"


def test_day_after_tomorrow_across_month():
    result = resolve("day after tomorrow", now=NOW2)
    assert result.resolved_date == date(2026, 12, 2)


def test_year_rollover():
    tomorrow = resolve("tomorrow", now=NOW3)
    assert tomorrow.resolved_date == date(2027, 1, 1)
    assert tomorrow.weekday == "Friday"


def test_yesterday_across_year_boundary():
    yesterday = resolve("yesterday", now=NOW3)
    assert yesterday.resolved_date == date(2026, 12, 30)


# --- weekdays -------------------------------------------------------------------

def test_bare_weekday_is_next_occurrence_not_today():
    friday = r("The deadline is Friday")
    assert friday.resolved_date == date(2026, 11, 13)          # Wed -> coming Friday


def test_bare_weekday_on_same_weekday_goes_next_week():
    friday = resolve("deadline Friday", now=NOW4)
    assert friday.resolved_date == date(2026, 11, 20)          # Friday -> +7, never today


def test_next_weekday_is_following_iso_week():
    assert r("deadline next Friday").resolved_date == date(2026, 11, 20)   # not Nov 13
    assert r("deadline next Monday").resolved_date == date(2026, 11, 16)


def test_last_weekday_is_strictly_past():
    assert r("started last Monday").resolved_date == date(2026, 11, 9)
    last_friday = resolve("last Friday", now=NOW4)
    assert last_friday.resolved_date == date(2026, 11, 6)      # never today


def test_this_weekday_is_current_iso_week():
    assert r("this Friday").resolved_date == date(2026, 11, 13)
    assert resolve("this Friday", now=NOW4).resolved_date == date(2026, 11, 13)  # today allowed


def test_sunday_resolution():
    assert r("deadline Sunday").resolved_date == date(2026, 11, 15)


# --- explicit dates ----------------------------------------------------------------

def test_explicit_date_with_year():
    result = r("Deadline is November 15, 2026, at 4 PM")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.resolved_date == date(2026, 11, 15)
    assert result.resolved_time.isoformat() == "16:00:00"
    assert result.weekday == "Sunday"
    assert result.assumptions == []
    assert result.matched_expression is not None


def test_explicit_date_day_first():
    assert r("15 November 2026").resolved_date == date(2026, 11, 15)


def test_explicit_date_ordinal():
    assert r("15th November 2026").resolved_date == date(2026, 11, 15)


def test_explicit_date_abbreviated_month():
    assert r("Nov 15 2026").resolved_date == date(2026, 11, 15)


def test_iso_date():
    result = r("deadline 2026-11-15")
    assert result.resolved_date == date(2026, 11, 15)
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION   # time still asked


# --- missing year ------------------------------------------------------------------

def test_missing_year_inferred_future_this_year():
    result = r("Deadline is November 15")
    assert result.resolved_date == date(2026, 11, 15)
    assert result.assumptions == ["year_inferred:2026"]


def test_missing_year_inferred_next_year_when_passed():
    result = r("Deadline is November 3")          # Nov 3 2026 already past Nov 11
    assert result.resolved_date == date(2027, 11, 3)
    assert result.assumptions == ["year_inferred:2027"]
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION  # date kept, time asked


# --- times -------------------------------------------------------------------------

def test_time_12h():
    assert r("at 4 PM").resolved_time.isoformat() == "16:00:00"


def test_time_12h_no_space():
    assert r("4pm").resolved_time.isoformat() == "16:00:00"


def test_time_24h():
    assert r("16:00").resolved_time.isoformat() == "16:00:00"


def test_time_with_minutes():
    assert r("10:30 AM").resolved_time.isoformat() == "10:30:00"


def test_noon_and_midnight():
    assert r("noon").resolved_time.isoformat() == "12:00:00"
    assert r("midnight").resolved_time.isoformat() == "00:00:00"


def test_12am_is_midnight_12pm_is_noon():
    assert r("12 AM").resolved_time.isoformat() == "00:00:00"
    assert r("12 PM").resolved_time.isoformat() == "12:00:00"


def test_time_without_date_clarifies_but_keeps_time():
    result = r("at 4 PM")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.resolved_time.isoformat() == "16:00:00"
    assert result.resolved_date is None
    assert result.clarification.missing == ["date"]


def test_bare_hour_without_meridiem_is_ambiguous():
    result = r("tomorrow at 5")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.resolved_date == date(2026, 11, 12)             # date preserved
    assert result.resolved_time is None
    assert result.clarification.reason == "ambiguous_time"
    assert result.clarification.missing == ["time"]


def test_colon_time_without_meridiem_is_24h():
    assert r("tomorrow at 17:30").resolved_time.isoformat() == "17:30:00"


# --- numeric dates -------------------------------------------------------------------

def test_unambiguous_day_first_numeric():
    result = r("15/11/2026")
    assert result.resolved_date == date(2026, 11, 15)


def test_unambiguous_month_first_numeric():
    assert r("11/15/2026").resolved_date == date(2026, 11, 15)


def test_ambiguous_numeric_date_requires_clarification():
    result = r("05/06/2026")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.resolved_date is None
    assert result.clarification.reason == "ambiguous_numeric_date"
    assert result.clarification.missing == ["date"]
    assert "June" in result.clarification.question and "May" in result.clarification.question


def test_ambiguous_numeric_date_resolves_with_configured_format():
    assert r("05/06/2026", date_format="DMY").resolved_date == date(2026, 6, 5)
    assert r("05/06/2026", date_format="MDY").resolved_date == date(2026, 5, 6)


def test_invalid_numeric_date():
    result = r("13/13/2026")
    assert result.status is ResolutionStatus.INVALID
    assert result.error is not None


# --- multiple candidates ---------------------------------------------------------------

def test_multiple_dates_require_clarification():
    result = r("today or tomorrow")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.clarification.reason == "multiple_candidates"
    assert result.clarification.missing == ["date"]


def test_multiple_times_require_clarification():
    result = r("tomorrow between 10 AM and 4 PM")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.clarification.reason == "multiple_candidates"
    assert result.clarification.missing == ["time"]
    assert result.resolved_date == date(2026, 11, 12)              # date still kept


def test_identical_duplicate_dates_count_once():
    result = r("tomorrow, 12 November 2026")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION   # date only -> time asked
    assert result.resolved_date == date(2026, 11, 12)


# --- invalid inputs ----------------------------------------------------------------------

def test_invalid_calendar_date():
    assert r("February 30").status is ResolutionStatus.INVALID


def test_invalid_explicit_leap_date():
    assert r("February 29, 2027").status is ResolutionStatus.INVALID


def test_invalid_hour_24h():
    assert r("tomorrow at 25:00").status is ResolutionStatus.INVALID


def test_invalid_meridiem_hour():
    assert r("13 PM").status is ResolutionStatus.INVALID


def test_invalid_minute():
    assert r("10:75").status is ResolutionStatus.INVALID


def test_empty_input():
    assert r("").status is ResolutionStatus.INVALID
    assert r("   ").status is ResolutionStatus.INVALID


# --- not found -----------------------------------------------------------------------------

def test_no_date_or_time_expression():
    result = r("Please handle the evening tasks sometime")
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.resolved_date is None and result.resolved_time is None


def test_unsupported_hinglish_relative_word_not_guessed():
    assert r("kal complete karo").status is ResolutionStatus.NOT_FOUND


# --- leap years ------------------------------------------------------------------------------

def test_feb_29_infers_next_leap_year():
    result = r("Deadline is February 29")
    assert result.resolved_date == date(2028, 2, 29)
    assert result.assumptions == ["year_inferred:2028"]


def test_feb_29_in_leap_year_explicit():
    assert r("February 29, 2028").resolved_date == date(2028, 2, 29)


def test_feb_29_numeric_in_leap_year():
    assert r("29/02/2028").resolved_date == date(2028, 2, 29)


# --- timezones ---------------------------------------------------------------------------------

def test_timezone_changes_local_date():
    utc_now = datetime(2026, 11, 11, 19, 0, tzinfo=ZoneInfo("UTC"))
    utc_result = resolve("today", now=utc_now, timezone="UTC")
    ist_result = resolve("today", now=utc_now, timezone="Asia/Kolkata")
    assert utc_result.resolved_date == date(2026, 11, 11)
    assert ist_result.resolved_date == date(2026, 11, 12)          # 00:30 IST next day
    assert ist_result.weekday == "Thursday"


def test_timezone_offset_in_iso_output():
    result = resolve("tomorrow at 10 AM", now=NOW, timezone="UTC")
    assert result.iso_datetime == "2026-11-12T10:00:00+00:00"


def test_dst_boundary_america_new_york():
    before = datetime(2026, 3, 7, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    est = resolve("today at 10 AM", now=before, timezone="America/New_York")
    edt = resolve("tomorrow at 10 AM", now=before, timezone="America/New_York")
    assert est.iso_datetime.endswith("-05:00")     # still standard time on Mar 7
    assert edt.iso_datetime.endswith("-04:00")     # daylight time on Mar 8


def test_unknown_timezone_is_invalid():
    result = r("today", timezone="Mars/Olympus")
    assert result.status is ResolutionStatus.INVALID
    assert "timezone" in result.error.lower()


# --- determinism & contract ----------------------------------------------------------------------

def test_same_inputs_same_output():
    a = r("Deadline is November 15 at 4 PM").to_dict()
    b = r("Deadline is November 15 at 4 PM").to_dict()
    assert a == b


def test_output_is_json_serializable():
    for text in ("November 15 at 4 PM", "tomorrow", "05/06/2026", "February 30", "evening"):
        payload = json.dumps(r(text).to_dict())
        assert isinstance(payload, str)


def test_weekday_matches_resolved_date():
    result = r("Deadline is November 15")                          # 2026-11-15
    assert result.resolved_date == date(2026, 11, 15)
    assert result.weekday == "Sunday"


def test_no_system_clock_access_when_now_injected():
    """A far-future injected clock must fully control the result."""
    far = datetime(2999, 1, 1, 8, 0, tzinfo=TZ)
    result = resolve("tomorrow", now=far)
    assert result.resolved_date == date(2999, 1, 2)


def test_system_clock_smoke_test():
    """Without now=, the resolver must still return a sane result using the real clock."""
    result = resolve("tomorrow at 10 AM", timezone="Asia/Kolkata")
    assert result.status is ResolutionStatus.RESOLVED


def test_date_only_preserves_date_and_marks_missing_time():
    result = r("Deadline is next Monday")
    assert result.status is ResolutionStatus.NEEDS_CLARIFICATION
    assert result.resolved_date == date(2026, 11, 16)
    assert result.weekday == "Monday"
    assert result.resolved_time is None
    assert result.iso_datetime is None
    assert result.clarification.missing == ["time"]


def test_invalid_date_format_config_raises():
    with pytest.raises(ValueError):
        DateResolver(date_format="YMD")
