"""Tests for ai/query_session.py: the pure TaskDraftEngine.

Uses a fake extractor (no Cohere calls) and a frozen clock
(NOW = Thu 2026-11-12 10:00 Asia/Kolkata).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ai.query_session import (
    STAGE_CONFIRM_FIELDS,
    STAGE_CONFIRM_REMINDER,
    STAGE_DONE,
    STAGE_FIELDS,
    STAGE_REMINDER,
    TaskDraftEngine,
)
from ai.text_extractor import ExtractionResult, TaskRecord

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)


class FakeExtractor:
    def __init__(self, result: ExtractionResult):
        self.result = result
        self.calls: list[str] = []

    def extract_draft(self, user_input: str) -> ExtractionResult:
        self.calls.append(user_input)
        return self.result


def full_extraction() -> ExtractionResult:
    """Everything stated explicitly except the status."""
    return ExtractionResult(
        task_title="Python API research",
        assigned_to="Rahul",
        assigned_when="10 November 2026 at 10:30 AM",
        deadline_when="15 November 2026 at 5 PM",
        reminder_when=None,
        status=None,
        progress=50,
    )


def vague_extraction() -> ExtractionResult:
    return ExtractionResult(
        task_title="Testing",
        assigned_to="Rohan",
        assigned_when=None,
        deadline_when="tomorrow",
        reminder_when=None,
        status=None,
        progress=None,
    )


def reminder_extraction() -> ExtractionResult:
    result = full_extraction()
    result.reminder_when = "30 minutes before the deadline"
    return result


def engine(extraction: ExtractionResult) -> TaskDraftEngine:
    return TaskDraftEngine(extractor=FakeExtractor(extraction), now=NOW)


# --- Phase A -------------------------------------------------------------------

def test_start_resolves_explicit_phrases():
    result = engine(full_extraction()).start("some input")

    draft = result.draft
    assert draft["task_title"] == "Python API research"
    assert draft["assigned_to"] == "Rahul"
    assert draft["assigned_date"] == "2026-11-10"
    assert draft["assigned_time"] == "10:30"
    assert draft["deadline_date"] == "2026-11-15"
    assert draft["deadline_time"] == "17:00"
    assert draft["progress"] == 50
    # status was not stated
    assert result.stage == STAGE_FIELDS
    assert result.missing_fields == ["status"]
    assert "status" in result.question.lower()


def test_start_resolves_vague_deadline_partially():
    result = engine(vague_extraction()).start("some input")

    assert result.stage == STAGE_FIELDS
    draft = result.draft
    assert draft["deadline_date"] == "2026-11-13"  # tomorrow, frozen clock
    assert draft["deadline_time"] is None
    assert set(result.missing_fields) >= {
        "assigned_date", "assigned_time", "deadline_time", "status", "progress"
    }


def test_apply_answers_completes_phase_a():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})

    assert result.stage == STAGE_CONFIRM_FIELDS


def test_invalid_status_answer_is_rejected():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "DONE"})

    assert result.stage == STAGE_FIELDS
    assert "must be PENDING" in result.question


def test_business_rule_violation_is_surfaced():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(
        result.draft, {"status": "COMPLETED", "progress": 40}
    )

    assert result.stage == STAGE_FIELDS
    assert "100%" in result.question


def test_vague_date_answer_resolves_via_resolver():
    engine_ = engine(vague_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"assigned_date": "next Monday"})

    draft = result.draft
    assert draft["assigned_date"] == "2026-11-16"  # next Monday after Wed Nov 11? no: after Thu Nov 12
    assert result.stage == STAGE_FIELDS


def test_progress_zero_is_not_treated_as_missing():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(
        result.draft, {"status": "PENDING", "progress": 0}
    )

    assert result.stage == STAGE_CONFIRM_FIELDS


def test_confirm_without_confirmation_stays():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=False)

    assert result.stage == STAGE_CONFIRM_FIELDS


def test_edit_at_confirmation_stage():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    # user edits progress at the confirmation stage
    result = engine_.apply(result.draft, {"progress": 80})

    assert result.stage == STAGE_CONFIRM_FIELDS
    assert result.draft["progress"] == 80


# --- Phase B (reminder) ----------------------------------------------------------

def test_confirm_fields_without_reminder_phrase_asks():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)

    assert result.stage == STAGE_REMINDER
    assert result.question


def test_prefilled_reminder_phrase_is_resolved_and_confirmed():
    engine_ = engine(reminder_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)

    # reminder was already stated by the user
    assert result.stage == STAGE_CONFIRM_REMINDER
    assert result.reminder["date"] == "2026-11-15"
    assert result.reminder["time"] == "16:30"


def test_relative_reminder_resolves_to_absolute():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    result = engine_.set_reminder(result.draft, "2 hours before the deadline")

    assert result.stage == STAGE_CONFIRM_REMINDER
    assert result.reminder["date"] == "2026-11-15"
    assert result.reminder["time"] == "15:00"


def test_past_reminder_is_reasked():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    result = engine_.set_reminder(result.draft, "yesterday at 5 pm")

    assert result.stage == STAGE_REMINDER
    assert "past" in result.question.lower()


def test_reminder_clarification_uses_context():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    # date only -> clarification carries the date, then a bare time completes it
    result = engine_.set_reminder(result.draft, "14 November 2026")
    assert result.stage == STAGE_REMINDER

    partial = result.draft["reminder_partial"]
    assert partial["date"] == "2026-11-14"

    result = engine_.set_reminder(result.draft, "5 pm")
    assert result.stage == STAGE_CONFIRM_REMINDER
    assert result.reminder["date"] == "2026-11-14"
    assert result.reminder["time"] == "17:00"


def test_confirm_reminder_false_reasks():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    result = engine_.set_reminder(result.draft, "2 hours before the deadline")
    result = engine_.confirm_reminder(result.draft, confirm=False)

    assert result.stage == STAGE_REMINDER


def test_full_flow_builds_task_record():
    engine_ = engine(reminder_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    assert result.stage == STAGE_CONFIRM_REMINDER
    result = engine_.confirm_reminder(result.draft, confirm=True)

    assert result.stage == STAGE_DONE
    assert result.done

    record = result.record
    assert isinstance(record, TaskRecord)
    assert record.task_title == "Python API research"
    assert record.assigned_date == date(2026, 11, 10)
    assert record.assigned_time == time(10, 30)
    assert record.deadline_date == date(2026, 11, 15)
    assert record.deadline_time == time(17, 0)
    assert record.status == "IN_PROGRESS"
    assert record.progress == 50
    assert record.reminder_date == date(2026, 11, 15)
    assert record.reminder_time == time(16, 30)


def test_draft_is_json_serialisable():
    import json

    engine_ = engine(reminder_extraction())
    result = engine_.start("input")
    result = engine_.apply(result.draft, {"status": "IN_PROGRESS"})
    result = engine_.confirm_fields(result.draft, confirm=True)
    result = engine_.confirm_reminder(result.draft, confirm=True)

    # must survive a stateless echo round-trip
    payload = json.dumps(result.draft)
    assert json.loads(payload) == result.draft
