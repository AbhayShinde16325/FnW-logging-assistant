"""Pure two-phase task draft engine.

Phase A: fill and confirm the 8 task fields.
Phase B: reminder (only after Phase A is confirmed).

No I/O happens here:
  - the extractor is injected (fake in tests, Cohere in production),
  - `now` is injectable (frozen clock in tests, real clock otherwise),
  - every step takes and returns a plain JSON-serialisable draft, so the
    same engine drives the stateless HTTP API, a CLI, and tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from datetime_resolver import ResolutionStatus, resolve

import config
from ai.reminder import ReminderError, parse_reminder
from ai.text_extractor import TaskRecord, validate_business_rules


FALLBACK_TIMEZONE = "Asia/Kolkata"

STAGE_START = "start"
STAGE_FIELDS = "fields"
STAGE_CONFIRM_FIELDS = "confirm_fields"
STAGE_REMINDER = "reminder"
STAGE_CONFIRM_REMINDER = "confirm_reminder"
STAGE_DONE = "done"

# Phase A fields, in the order they are asked about.
FIELD_ORDER = [
    "task_title",
    "assigned_to",
    "assigned_date",
    "assigned_time",
    "deadline_date",
    "deadline_time",
    "status",
    "progress",
]

FIELD_LABELS = {
    "task_title": "task title",
    "assigned_to": "assignee",
    "assigned_date": "assigned date",
    "assigned_time": "assigned time",
    "deadline_date": "deadline date",
    "deadline_time": "deadline time",
    "status": "status",
    "progress": "progress",
}

DATE_FIELDS = {"assigned_date": "assigned_time", "deadline_date": "deadline_time"}
TIME_FIELDS = {"assigned_time": "assigned_date", "deadline_time": "deadline_date"}
TEXT_FIELDS = {"task_title", "assigned_to"}
STATUS_VALUES = {"PENDING", "IN_PROGRESS", "COMPLETED"}

DEFAULT_REMINDER_QUESTION = (
    "When should the reminder be? Give a date and time (e.g. '15 November 2026 "
    "at 4 PM') or say how long before the deadline (e.g. '30 minutes before "
    "the deadline')."
)

STATUS_PHRASES = {
    "pending": "PENDING",
    "not started": "PENDING",
    "not_started": "PENDING",
    "todo": "PENDING",
    "in progress": "IN_PROGRESS",
    "in_progress": "IN_PROGRESS",
    "ongoing": "IN_PROGRESS",
    "working on it": "IN_PROGRESS",
    "completed": "COMPLETED",
    "complete": "COMPLETED",
    "done": "COMPLETED",
    "finished": "COMPLETED",
}

NUMBER_WORDS = {
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}


def _to_date(value: str) -> date:
    return date.fromisoformat(value)


def _to_time(value: str) -> time:
    parts = value.split(":")
    return time(int(parts[0]), int(parts[1]))


@dataclass
class StepResult:
    """Outcome of one engine step (mapped to an API response by the service)."""

    stage: str
    draft: dict
    missing_fields: list = field(default_factory=list)
    question: str | None = None
    message: str | None = None
    reminder: dict | None = None
    suggested_reminder: dict | None = None
    done: bool = False
    record: TaskRecord | None = None


class TaskDraftEngine:
    def __init__(self, extractor, now: datetime | None = None, timezone: str | None = None):
        self.extractor = extractor
        self._fixed_now = now
        self.timezone = timezone or getattr(config, "TIMEZONE", FALLBACK_TIMEZONE)

    # ------------------------------------------------------------------ steps

    def start(self, user_input: str) -> StepResult:
        extraction = self.extractor.extract_draft(user_input)

        draft = self._empty_draft()
        draft["raw"] = {
            "assigned_when": extraction.assigned_when,
            "deadline_when": extraction.deadline_when,
            "reminder_when": extraction.reminder_when,
        }

        if extraction.task_title:
            draft["task_title"] = extraction.task_title.strip()
        if extraction.assigned_to:
            draft["assigned_to"] = extraction.assigned_to.strip()
        if extraction.status:
            draft["status"] = extraction.status
        if extraction.progress is not None:
            draft["progress"] = extraction.progress

        questions: dict[str, str] = {}
        for kind in ("assigned", "deadline"):
            phrase = draft["raw"].get(f"{kind}_when")
            if not phrase:
                continue

            resolved_date, resolved_time, question = self._resolve_phrase(phrase)
            if resolved_date:
                draft[f"{kind}_date"] = resolved_date.isoformat()
            if resolved_time:
                draft[f"{kind}_time"] = resolved_time.strftime("%H:%M")
            if question:
                target = f"{kind}_date" if resolved_date is None else f"{kind}_time"
                questions[target] = question

        if questions:
            draft["_questions"] = questions

        return self._next_step(draft)

    def apply(self, draft: dict, answers: dict | None) -> StepResult:
        draft = dict(draft or {})
        errors: list[str] = []

        for name, raw_value in (answers or {}).items():
            if name not in FIELD_ORDER:
                continue

            if raw_value is None or str(raw_value).strip() == "":
                draft[name] = None  # explicit clearing (edit mode)
                continue

            try:
                draft[name] = self._parse_field(name, str(raw_value).strip())
            except ValueError as error:
                errors.append(f"{FIELD_LABELS[name]}: {error}")

        rule_error = self._check_rules(draft)
        if rule_error:
            errors.append(rule_error)

        return self._next_step(draft, errors=errors)

    def answer(self, draft: dict, user_input: str) -> StepResult:
        """Interpret one conversational reply and fill the missing fields.

        The reply is passed through the same extractor used for the initial
        message, so the user can answer naturally ("it's due next Friday at
        5 pm", "Rahul", "50 percent done") instead of filling a form.
        """

        draft = dict(draft or {})
        target = self._first_missing(draft)

        extraction = self.extractor.extract_draft(user_input)
        answers = self._extraction_answers(
            extraction, draft, target=target
        )

        if target is not None and target not in answers:
            # If the reply clearly answers a different date field (for
            # example the user volunteers the deadline while we asked for the
            # assigned date), keep that value and re-ask the original field
            # instead of storing unrelated text in the wrong field.
            conflicts = self._conflicts_with_target(target, user_input)

            if not (answers and conflicts):
                value = self._fallback_answer(target, user_input, extraction)
                if value is not None:
                    answers[target] = value

        return self.apply(draft, answers)

    def edit(self, draft: dict, user_input: str) -> StepResult:
        """Apply a natural-language correction at the confirmation stage."""

        draft = dict(draft or {})
        extraction = self.extractor.extract_draft(user_input)
        answers = self._extraction_answers(
            extraction, draft, include_existing=True
        )

        if not answers:
            return self._next_step(
                draft,
                errors=[
                    "I couldn't find a field to update. Tell me the field and "
                    "its new value, for example 'progress is 80 percent'."
                ],
            )

        return self.apply(draft, answers)

    def confirm_fields(self, draft: dict, confirm: bool) -> StepResult:
        draft = dict(draft or {})

        if not confirm:
            return StepResult(
                STAGE_CONFIRM_FIELDS,
                draft=draft,
                message="Review the details, edit if needed, then confirm.",
            )

        rule_error = self._check_rules(draft)
        if rule_error:
            return StepResult(STAGE_FIELDS, draft=draft, question=rule_error)

        return self._begin_reminder(draft)

    def set_reminder(self, draft: dict, reminder_text: str) -> StepResult:
        draft = dict(draft or {})
        context = draft.pop("reminder_partial", None)

        try:
            reminder_date, reminder_time = parse_reminder(
                reminder_text,
                deadline_date=_to_date(draft["deadline_date"]),
                deadline_time=_to_time(draft["deadline_time"]),
                now=self._now(),
                timezone=self.timezone,
                context=context,
            )
        except ReminderError as error:
            partial = {}
            if error.resolved_date:
                partial["date"] = error.resolved_date.isoformat()
            if error.resolved_time:
                partial["time"] = error.resolved_time.strftime("%H:%M")
            if partial:
                draft["reminder_partial"] = partial

            return StepResult(STAGE_REMINDER, draft=draft, question=str(error))

        draft["reminder_date"] = reminder_date.isoformat()
        draft["reminder_time"] = reminder_time.strftime("%H:%M")

        return StepResult(
            STAGE_CONFIRM_REMINDER,
            draft=draft,
            reminder=self._reminder_view(draft),
            message=(
                f"Reminder set for {self._reminder_display(reminder_date, reminder_time)}. "
                "Is that correct?"
            ),
        )

    def confirm_reminder(self, draft: dict, confirm: bool) -> StepResult:
        draft = dict(draft or {})

        if not confirm:
            return StepResult(
                STAGE_REMINDER, draft=draft, question=DEFAULT_REMINDER_QUESTION
            )

        record = self._build_record(draft)

        return StepResult(
            STAGE_DONE, draft=draft, done=True, record=record,
            message="Task ready to save.",
        )

    # ---------------------------------------------------------------- helpers

    def _now(self) -> datetime:
        if self._fixed_now is not None:
            return self._fixed_now
        return datetime.now(ZoneInfo(self.timezone))

    @staticmethod
    def _empty_draft() -> dict:
        draft = {name: None for name in FIELD_ORDER}
        draft["reminder_date"] = None
        draft["reminder_time"] = None
        return draft

    @staticmethod
    def _first_missing(draft: dict) -> str | None:
        for name in FIELD_ORDER:
            if draft.get(name) is None:
                return name
        return None

    def _extraction_answers(
        self,
        extraction,
        draft: dict,
        target: str | None = None,
        include_existing: bool = False,
    ) -> dict:
        """Turn an extraction into {field: value} answers.

        When filling a single missing field we only take newly missing fields
        from the reply, so an earlier answer is never overwritten by chance.
        """

        def wants(name: str) -> bool:
            if include_existing:
                return True
            return name == target or draft.get(name) is None

        answers: dict[str, object] = {}

        if extraction.task_title and wants("task_title"):
            answers["task_title"] = extraction.task_title.strip()

        if extraction.assigned_to and wants("assigned_to"):
            answers["assigned_to"] = extraction.assigned_to.strip()

        if extraction.status and wants("status"):
            answers["status"] = extraction.status

        if extraction.progress is not None and wants("progress"):
            answers["progress"] = extraction.progress

        for kind in ("assigned", "deadline"):
            phrase = getattr(extraction, f"{kind}_when", None)
            if not phrase:
                continue

            resolved_date, resolved_time, _ = self._resolve_phrase(phrase)

            if resolved_date and wants(f"{kind}_date"):
                answers[f"{kind}_date"] = resolved_date.isoformat()

            if resolved_time and wants(f"{kind}_time"):
                answers[f"{kind}_time"] = resolved_time.strftime("%H:%M")

        return answers

    @staticmethod
    def _conflicts_with_target(name: str, user_input: str) -> bool:
        if name not in DATE_FIELDS and name not in TIME_FIELDS:
            return False

        lowered = (user_input or "").lower()

        if name.startswith("deadline"):
            return "assigned" in lowered or "assign " in lowered

        return "deadline" in lowered or "due " in lowered

    def _fallback_answer(self, name: str, user_input: str, extraction):
        """Map a bare reply to the field the assistant just asked about."""

        text = (user_input or "").strip()

        if name in TEXT_FIELDS:
            value = getattr(extraction, name, None)
            return value.strip() if value else text or None

        if name == "status":
            return extraction.status or self._status_from_text(text)

        if name == "progress":
            if extraction.progress is not None:
                return extraction.progress
            return self._progress_from_text(text)

        if name in DATE_FIELDS or name in TIME_FIELDS:
            return text or None

        return text or None

    @staticmethod
    def _status_from_text(text: str) -> str | None:
        normalized = " ".join((text or "").lower().replace(",", " ").split())

        for phrase, status in STATUS_PHRASES.items():
            if phrase in normalized:
                return status

        upper = normalized.replace(" ", "_").upper()
        if upper in STATUS_VALUES:
            return upper

        return None

    @staticmethod
    def _progress_from_text(text: str) -> int | None:
        cleaned = (text or "").strip()

        if re.fullmatch(r"\d{1,3}\s*%?", cleaned):
            value = int(re.sub(r"\D", "", cleaned))
            if 0 <= value <= 100:
                return value

        match = re.search(r"(\d{1,3})\s*(?:%|percent|pct|per cent)", cleaned.lower())
        if match:
            value = int(match.group(1))
            if 0 <= value <= 100:
                return value

        normalized = cleaned.lower()
        if "percent" in normalized or "%" in normalized:
            for word, value in NUMBER_WORDS.items():
                if re.search(rf"\b{word}\b", normalized):
                    return value

        return None

    def _resolve_phrase(self, phrase: str):
        result = resolve(phrase, now=self._now(), timezone=self.timezone)

        if result.status == ResolutionStatus.RESOLVED:
            return result.resolved_date, result.resolved_time, None

        if result.status == ResolutionStatus.NEEDS_CLARIFICATION:
            question = result.clarification.question if result.clarification else None
            return result.resolved_date, result.resolved_time, question

        return None, None, None

    def _parse_field(self, name: str, value: str):
        if name in TEXT_FIELDS:
            return value

        if name == "status":
            upper = value.upper()
            if upper not in STATUS_VALUES:
                raise ValueError("must be PENDING, IN_PROGRESS, or COMPLETED.")
            return upper

        if name == "progress":
            try:
                number = int(value)
            except ValueError:
                raise ValueError("must be a number between 0 and 100.")
            if not 0 <= number <= 100:
                raise ValueError("must be between 0 and 100.")
            return number

        if name in DATE_FIELDS:
            return self._parse_date_value(value)

        return self._parse_time_value(value)

    def _parse_date_value(self, value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass

        result = resolve(value, now=self._now(), timezone=self.timezone)

        if result.status == ResolutionStatus.RESOLVED and result.resolved_date:
            return result.resolved_date.isoformat()

        if result.status == ResolutionStatus.NEEDS_CLARIFICATION and result.resolved_date:
            # Partial fill: date known, time asked separately.
            return result.resolved_date.isoformat()

        if result.status == ResolutionStatus.INVALID:
            raise ValueError(result.error or "invalid date.")

        raise ValueError(
            "could not understand the date. Try '2026-11-15' or 'next Monday'."
        )

    def _parse_time_value(self, value: str) -> str:
        result = resolve(value, now=self._now(), timezone=self.timezone)

        if result.status == ResolutionStatus.RESOLVED and result.resolved_time:
            return result.resolved_time.strftime("%H:%M")

        if result.status == ResolutionStatus.NEEDS_CLARIFICATION and result.resolved_time:
            return result.resolved_time.strftime("%H:%M")

        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"

        if result.status == ResolutionStatus.INVALID:
            raise ValueError(result.error or "invalid time.")

        raise ValueError("could not understand the time. Try '17:30' or '5 PM'.")

    @staticmethod
    def _check_rules(draft: dict) -> str | None:
        status = draft.get("status")
        progress = draft.get("progress")
        if status is None or progress is None:
            return None

        if status == "COMPLETED" and progress != 100:
            return "Completed tasks must have 100% progress."

        if progress > 0 and status == "PENDING":
            return "Tasks with progress must be IN_PROGRESS or COMPLETED."

        return None

    def _next_step(self, draft: dict, errors: list[str] | None = None) -> StepResult:
        draft = dict(draft)
        stored_questions = draft.pop("_questions", None) or {}

        missing = [name for name in FIELD_ORDER if draft.get(name) is None]

        if errors:
            question = "  ".join(errors)
            return StepResult(
                STAGE_FIELDS, draft=draft, missing_fields=missing, question=question
            )

        if missing:
            first = missing[0]
            question = stored_questions.get(first) or self._default_question(first, draft)
            return StepResult(
                STAGE_FIELDS, draft=draft, missing_fields=missing, question=question
            )

        return StepResult(
            STAGE_CONFIRM_FIELDS,
            draft=draft,
            message="Please confirm the task details. You can edit any field before continuing.",
        )

    @staticmethod
    def _default_question(name: str, draft: dict) -> str:
        if name in TIME_FIELDS and draft.get(TIME_FIELDS[name]):
            return (
                f"What time on {draft[TIME_FIELDS[name]]} should the "
                f"{FIELD_LABELS[name]} be?"
            )
        return f"What is the {FIELD_LABELS[name]}?"

    def _begin_reminder(self, draft: dict) -> StepResult:
        draft = dict(draft)
        reminder_when = (draft.get("raw") or {}).get("reminder_when")

        parsed = None
        if reminder_when:
            try:
                parsed = parse_reminder(
                    reminder_when,
                    deadline_date=_to_date(draft["deadline_date"]),
                    deadline_time=_to_time(draft["deadline_time"]),
                    now=self._now(),
                    timezone=self.timezone,
                )
            except ReminderError:
                parsed = None

        if parsed:
            reminder_date, reminder_time = parsed
            draft["reminder_date"] = reminder_date.isoformat()
            draft["reminder_time"] = reminder_time.strftime("%H:%M")

            return StepResult(
                STAGE_CONFIRM_REMINDER,
                draft=draft,
                reminder=self._reminder_view(draft),
                message=(
                    f"You mentioned a reminder on "
                    f"{self._reminder_display(reminder_date, reminder_time)}. "
                    "Is that correct?"
                ),
            )

        return StepResult(STAGE_REMINDER, draft=draft, question=DEFAULT_REMINDER_QUESTION)

    def _build_record(self, draft: dict) -> TaskRecord:
        return TaskRecord(
            task_title=draft["task_title"],
            assigned_to=draft.get("assigned_to"),
            assigned_date=_to_date(draft["assigned_date"]),
            assigned_time=_to_time(draft["assigned_time"]),
            deadline_date=_to_date(draft["deadline_date"]),
            deadline_time=_to_time(draft["deadline_time"]),
            status=draft["status"],
            progress=int(draft["progress"]),
            reminder_date=_to_date(draft["reminder_date"]),
            reminder_time=_to_time(draft["reminder_time"]),
        )

    @staticmethod
    def _reminder_view(draft: dict) -> dict:
        reminder_date = _to_date(draft["reminder_date"])
        return {
            "date": draft["reminder_date"],
            "time": draft["reminder_time"],
            "display": TaskDraftEngine._reminder_display(
                reminder_date, draft["reminder_time"]
            ),
        }

    @staticmethod
    def _reminder_display(reminder_date: date, reminder_time) -> str:
        if hasattr(reminder_time, "strftime"):
            reminder_time = reminder_time.strftime("%H:%M")
        return f"{reminder_date.strftime('%d %B %Y (%A)')} at {reminder_time}"
