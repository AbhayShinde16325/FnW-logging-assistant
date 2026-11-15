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
    TaskUpdateEngine,
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


def test_completed_status_syncs_progress_to_100():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(
        result.draft, {"status": "COMPLETED", "progress": 40}
    )

    assert result.stage == STAGE_CONFIRM_FIELDS
    assert result.draft["status"] == "COMPLETED"
    assert result.draft["progress"] == 100


def test_pending_status_with_progress_syncs_to_in_progress():
    engine_ = engine(full_extraction())
    result = engine_.start("input")
    result = engine_.apply(
        result.draft, {"status": "PENDING", "progress": 50}
    )

    assert result.stage == STAGE_CONFIRM_FIELDS
    assert result.draft["status"] == "IN_PROGRESS"
    assert result.draft["progress"] == 50


def test_single_shot_validation_uses_the_same_sync_rule():
    from ai.text_extractor import TaskDetails, validate_business_rules

    task = TaskDetails(task_title="Python API research", status="PENDING", progress=50)
    validate_business_rules(task)
    assert task.status == "IN_PROGRESS"
    assert task.progress == 50

    task = TaskDetails(task_title="Python API research", status="COMPLETED", progress=40)
    validate_business_rules(task)
    assert task.status == "COMPLETED"
    assert task.progress == 100


class EmptyExtractor:
    def extract_draft(self, user_input: str) -> ExtractionResult:
        return ExtractionResult()


def test_all_fields_filled_correction_escapes_validation_loop():
    engine_ = TaskDraftEngine(extractor=EmptyExtractor(), now=NOW)
    draft = engine(full_extraction()).start("input").draft
    draft["status"] = "PENDING"

    result = engine_.answer(
        draft, "the status is in progress and the progress is 40 percent"
    )

    assert result.stage == STAGE_CONFIRM_FIELDS
    assert result.draft["status"] == "IN_PROGRESS"
    assert result.draft["progress"] == 40


def test_all_fields_filled_accepts_bare_status_reply():
    engine_ = TaskDraftEngine(extractor=EmptyExtractor(), now=NOW)
    draft = engine(full_extraction()).start("input").draft
    draft["status"] = "PENDING"

    result = engine_.answer(draft, "IN_PROGRESS")

    assert result.stage == STAGE_CONFIRM_FIELDS
    assert result.draft["status"] == "IN_PROGRESS"


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


# --- update task selection -----------------------------------------------------


SELECTION_DRAFT = {
    "intent": "UPDATE_TASK",
    "candidates": [
        {"id": 2460, "task_title": "mcp server study"},
        {"id": 2461, "task_title": "I want to add a new task"},
        {"id": 2462, "task_title": "CSS designing"},
    ],
    "changes": {"deadline_date": "2026-12-03"},
}


def test_selection_understands_a_short_title_reference():
    engine_ = TaskUpdateEngine(now=NOW)

    selection = engine_.resolve_selection(SELECTION_DRAFT, "css task")

    assert selection == {
        "task_id": 2462,
        "changes": {"deadline_date": "2026-12-03"},
    }


def test_selection_does_not_pick_a_task_for_generic_words():
    engine_ = TaskUpdateEngine(now=NOW)

    assert engine_.resolve_selection(SELECTION_DRAFT, "the task") is None
    assert engine_.resolve_selection(SELECTION_DRAFT, "my task") is None


def test_selection_reports_equally_good_matches_as_ambiguous():
    engine_ = TaskUpdateEngine(now=NOW)
    draft = {
        "intent": "UPDATE_TASK",
        "candidates": [
            {"id": 1, "task_title": "html and css learning"},
            {"id": 2, "task_title": "CSS designing"},
        ],
        "changes": {},
    }

    assert engine_.resolve_selection(draft, "css task") is None
