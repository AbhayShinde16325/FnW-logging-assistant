"""Validated semantic task-property mapping.

The assistant understands a question such as "What is its reminder date?"
semantically. This module maps that interpretation onto the real columns of
``database.models.Task`` and formats stored values for a concise answer.

It never executes SQL. The repository validates the resulting columns against
the ORM model before it reads anything, so a model can never invent a column.
"""

from __future__ import annotations

import re
from datetime import date, time

from database.models import Task

TASK_COLUMNS = frozenset(column.name for column in Task.__table__.columns)

FIELD_COLUMNS = {
    "id": ("id",),
    "task_title": ("task_title",),
    "task_description": ("task_description",),
    "description": ("task_description",),
    "expected_outcome": ("task_description",),
    "assigned_to": ("assigned_to",),
    "assigned_date": ("assigned_date",),
    "assigned_time": ("assigned_time",),
    "assigned": ("assigned_date", "assigned_time"),
    "deadline_date": ("deadline_date",),
    "deadline_time": ("deadline_time",),
    "deadline": ("deadline_date", "deadline_time"),
    "reminder_date": ("reminder_date",),
    "reminder_time": ("reminder_time",),
    "reminder": ("reminder_date", "reminder_time"),
    "status": ("status",),
    "progress": ("progress",),
}

DATE_FIELDS = {
    "assigned_date",
    "deadline_date",
    "reminder_date",
    "assigned",
    "deadline",
    "reminder",
}
TIME_FIELDS = {"assigned_time", "deadline_time", "reminder_time"}
TEXT_FIELDS = {
    "id", "task_title", "task_description", "assigned_to", "status"
}
INTEGER_FIELDS = {"progress"}

FULL_DETAILS = "full"

NULL_MESSAGES = {
    "progress": "No progress has been set for this task.",
    "status": "No status has been set for this task.",
    "assigned_to": "No assignee has been set for this task.",
    "task_title": "No title has been set for this task.",
    "task_description": "No description has been set for this task.",
    "description": "No description has been set for this task.",
    "expected_outcome": "No description has been set for this task.",
    "assigned_date": "No assigned date has been set for this task.",
    "assigned_time": "No assigned time has been set for this task.",
    "assigned": "No assigned date has been set for this task.",
    "deadline_date": "No deadline date has been set for this task.",
    "deadline_time": "No deadline time has been set for this task.",
    "deadline": "No deadline date has been set for this task.",
    "reminder_date": "No reminder date has been set.",
    "reminder_time": "No reminder time has been set.",
    "reminder": "No reminder date has been set.",
}

_FULL_DETAILS_PATTERN = re.compile(
    r"\b(all|full|complete|entire|whole)\b[^.]{0,40}\b(details?|information|info|record)\b"
    r"|\beverything\b[^.]{0,30}\b(task|about)\b"
    r"|\btask\s+details\b"
    r"|\bshow\s+(me\s+)?(the\s+)?(whole|entire)\s+task\b",
    re.IGNORECASE,
)

_ALIASES = (
    ("reminder_time", (
        r"\breminder\s+time\b",
        r"\bnotification\s+time\b",
        r"\btime\s+(?:of|for)\s+the\s+reminder\b",
        r"\breminder\b[^.]{0,20}\bat\s+what\s+time\b",
    )),
    ("reminder_date", (
        r"\breminder\s+(?:date|day)\b",
        r"\bnotification\s+(?:date|day)\b",
        r"\bdate\s+(?:of|for)\s+the\s+reminder\b",
        r"\bwhen\s+(?:should|will|do|would)\b[^.]{0,30}\bremind",
        r"\bwhen\s+(?:should|will|do|would)\b[^.]{0,30}\breminder\b",
    )),
    ("reminder", (
        r"\breminder\b",
        r"\bnotification\b",
    )),
    ("deadline_time", (
        r"\bdeadline\s+time\b",
        r"\bdue\s+time\b",
        r"\bsubmission\s+time\b",
        r"\btime\s+(?:of|for)\s+the\s+deadline\b",
    )),
    ("deadline_date", (
        r"\bdeadline\s+(?:date|day)\b",
        r"\bdue\s+date\b",
        r"\bsubmission\s+date\b",
        r"\bdate\s+(?:of|for)\s+the\s+deadline\b",
    )),
    ("deadline", (
        r"\bdeadline\b",
        r"\bdue\b",
        r"\bsubmission\b",
        r"\bwhen\s+(?:is|will)\b[^.]{0,20}\bdue\b",
        r"\bwhen\s+(?:is|will)\b[^.]{0,20}\bdeadline\b",
    )),
    ("assigned_time", (
        r"\bassigned\s+time\b",
        r"\bassignment\s+time\b",
        r"\btime\s+(?:of|for)\s+(?:the\s+)?assign(?:ment|ing)\b",
        r"\bwhat\s+time\b[^.]{0,20}\bassign",
    )),
    ("assigned_date", (
        r"\bassigned\s+(?:date|day)\b",
        r"\bassignment\s+date\b",
        r"\bdate\s+(?:of|for)\s+(?:the\s+)?assign(?:ment|ing)\b",
        r"\bwhen\s+(?:was|were|is|did)\b[^.]{0,30}\bassign",
    )),
    ("assigned_to", (
        r"\bassigned\s+to\b",
        r"\bassignee\b",
        r"\bowner\b",
        r"\bresponsible\b",
        r"\bwho\s+(?:is|was)\b[^.]{0,30}\bassign",
        r"\bwho\s+is\s+responsible\b",
    )),
    ("progress", (
        r"\bprogress\b",
        r"\bcompletion\b",
        r"\bpercent(?:age)?\b",
        r"\bhow\s+much\b[^.]{0,30}\b(?:done|complete|completed)\b",
        r"\bhow\s+much\s+work\b[^.]{0,30}\b(?:left|remaining|remains)\b",
        r"\bwork\s+remains\b",
        r"\bcompleted\s+so\s+far\b",
        r"\bdone\s+so\s+far\b",
    )),
    ("status", (
        r"\bstatus\b",
        r"\bstate\b",
        r"\bstage\b",
        r"\bis\s+it\s+(?:pending|in\s+progress|completed|done|finished)\b",
        r"\bhas\s+it\s+been\s+completed\b",
        r"\bis\s+it\s+done\b",
    )),
    ("task_title", (
        r"\btitle\b",
        r"\btask\s+name\b",
        r"\bname\s+of\s+the\s+task\b",
        r"\bwhat\s+is\s+the\s+task\b",
    )),
    ("task_description", (
        r"\bdescription\b",
        r"\bdescribed\b",
        r"\bexpected\s+outcome\b",
        r"\boutcome\b",
        r"\bwhat\s+(?:needs|has)\s+to\s+be\s+done\b",
    )),
    ("id", (
        r"\btask\s+id\b",
        r"\bid\s+number\b",
        r"\bwhich\s+task\s+id\b",
    )),
    ("assigned", (
        r"\bassigned\b",
        r"\bassignment\b",
    )),
)

_COMPILED_ALIASES = tuple(
    (field, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for field, patterns in _ALIASES
)


def normalize_field(field: str | None) -> str | None:
    """Map a semantic id or a real column name to a validated field."""

    text = (field or "").strip().lower()

    if not text:
        return None

    if text in {FULL_DETAILS, "details", "all", "task_details", "all_details"}:
        return FULL_DETAILS

    if text in FIELD_COLUMNS:
        return text

    return resolve_requested_field(text)


def resolve_requested_fields(text: str | None) -> list[str]:
    """Every distinct semantic field a message asks about, in alias order."""

    if not text:
        return []

    if _FULL_DETAILS_PATTERN.search(text):
        return [FULL_DETAILS]

    fields = []

    for field, patterns in _COMPILED_ALIASES:
        if any(pattern.search(text) for pattern in patterns):
            fields.append(field)

    # A specific alias ("deadline date") also matches the generic alias
    # ("deadline"). Keep only the specific fields so a single-field question
    # is not mistaken for a multi-field one.
    generic = {"assigned", "deadline", "reminder"}
    specific = {
        "assigned_to",
        "assigned_date",
        "assigned_time",
        "deadline_date",
        "deadline_time",
        "reminder_date",
        "reminder_time",
    }

    if any(field in specific for field in fields):
        fields = [
            field
            for field in fields
            if not (field in generic and any(
                other.startswith(field)
                for other in fields
                if other in specific
            ))
        ]

    return list(dict.fromkeys(fields))


def resolve_requested_field(text: str | None) -> str | None:
    """The first semantic field a message asks about, or ``None``."""

    fields = resolve_requested_fields(text)
    return fields[0] if fields else None


def is_full_details_request(text: str | None) -> bool:
    return resolve_requested_field(text) == FULL_DETAILS


def field_columns(field: str | None) -> tuple[str, ...]:
    """The real Task columns of a validated semantic field."""

    normalized = normalize_field(field)

    if normalized is None or normalized == FULL_DETAILS:
        raise ValueError("The requested field is not available in the task database.")

    columns = FIELD_COLUMNS[normalized]

    if any(column not in TASK_COLUMNS for column in columns):
        raise ValueError("The requested field is not available in the task database.")

    return columns


def _value(task, name: str):
    if isinstance(task, dict):
        return task.get(name)

    return getattr(task, name, None)


def _format_date(value) -> str:
    if isinstance(value, date):
        return value.strftime("%d %B %Y")

    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value).strftime("%d %B %Y")
        except ValueError:
            return value

    return str(value)


def _format_time(value) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")

    if isinstance(value, str) and value:
        return value[:5]

    return str(value)


def _primary_column(field: str) -> str:
    columns = FIELD_COLUMNS[field]

    for column in columns:
        if column.endswith("_date"):
            return column

    for column in columns:
        if column.endswith("_time"):
            return column

    return columns[0]


def format_field_value(field: str, task) -> str:
    """Format the actual stored value of one field for a short answer."""

    normalized = normalize_field(field)

    if normalized is None or normalized == FULL_DETAILS:
        raise ValueError("The requested field is not available in the task database.")

    if normalized == "progress":
        value = _value(task, "progress")
        return NULL_MESSAGES["progress"] if value is None else f"{int(value)}%"

    if normalized == "status":
        value = _value(task, "status")
        return NULL_MESSAGES["status"] if value is None else str(value)

    if normalized in (
        "task_title",
        "task_description",
        "description",
        "expected_outcome",
        "assigned_to",
        "id",
    ):
        value = _value(task, _primary_column(normalized))
        return NULL_MESSAGES.get(normalized, "Not set.") if value is None else str(value)

    if normalized in DATE_FIELDS:
        column = _primary_column(normalized)
        value = _value(task, column)

        if value is None:
            return NULL_MESSAGES.get(normalized, "Not set.")

        return _format_date(value)

    if normalized in TIME_FIELDS:
        value = _value(task, normalized)

        if value is None:
            return NULL_MESSAGES.get(normalized, "Not set.")

        return _format_time(value)

    return str(_value(task, normalized))
