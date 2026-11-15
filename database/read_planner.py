"""Schema-aware read planning for the ``tasks`` table.

The planner is the *structured* equivalent of an ad-hoc SQL query. It does
not execute anything and it never accepts free-form SQL from a model:

1. a request is described as a small plan (columns, filters, order, limit),
2. the plan is validated against the ORM model (the schema),
3. the plan is compiled into one parameterized ``SELECT`` statement,
4. the shared execution layer validates the statement again.

The module is pure. The optional LLM "brain" lives in
``services.agentic_read`` and only produces a plan of this shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from database import search as task_search
from database.models import Task

TASK_TABLE = "tasks"

MODEL_COLUMNS = tuple(column.name for column in Task.__table__.columns)
READABLE_COLUMNS = frozenset(MODEL_COLUMNS)

STATUS_VALUES = ("PENDING", "IN_PROGRESS", "COMPLETED")
# "Pending" in ordinary language usually means "not finished yet". The
# explicit "status is pending" wording still means the exact PENDING value.
OPEN_STATUSES = ("PENDING", "IN_PROGRESS")

DATE_COLUMNS = frozenset(
    {"assigned_date", "deadline_date", "reminder_date"}
)
TIME_COLUMNS = frozenset(
    {"assigned_time", "deadline_time", "reminder_time"}
)
TEXT_COLUMNS = frozenset({"task_title", "assigned_to", "status"})
INTEGER_COLUMNS = frozenset({"id", "progress"})

ALLOWED_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "in",
        "contains",
        "starts_with",
        "gt",
        "gte",
        "lt",
        "lte",
        "is_null",
        "not_null",
    }
)

_RANGE_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_TEXT_OPERATORS = frozenset({"contains", "starts_with"})
_NULL_OPERATORS = frozenset({"is_null", "not_null"})

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# Words that appear in a list request but name no actual task.
_COMMAND_WORDS = frozenset(
    {
        "gather", "get", "fetch", "return", "display", "print", "provide",
        "collect", "retrieve", "pull", "bring", "obtain", "read", "list",
        "show", "give", "tell", "find", "search", "look", "check", "all",
        "every", "each", "any", "some", "my", "mine", "our", "me",
        "please", "now", "currently", "there", "are", "is", "the", "a",
        "an", "of", "from", "with", "for", "to", "in", "on", "that",
        "which", "who", "whose", "what", "status", "state", "tasks", "task",
        "todo", "todos", "item", "items",
    }
)
_STATUS_WORDS = frozenset(
    {
        "pending", "outstanding", "open", "unfinished", "incomplete",
        "remaining", "active", "ongoing", "progress", "in_progress",
        "completed", "complete", "done", "finished", "closed",
    }
)
_COMMAND_STEMS = frozenset(task_search.stem(word) for word in _COMMAND_WORDS)
_STATUS_STEMS = frozenset(task_search.stem(word) for word in _STATUS_WORDS)

_OPEN_PATTERN = re.compile(
    r"\b(pending|outstanding|open|unfinished|incomplete|remaining|"
    r"not\s+(?:yet\s+)?(?:completed|done|finished)|yet\s+to\s+(?:start|"
    r"complete|finish)|in\s+progress|ongoing|active|unresolved|"
    r"still\s+(?:open|pending|running))\b",
    re.IGNORECASE,
)
_IN_PROGRESS_PATTERN = re.compile(
    r"\b(in\s+progress|in-progress|ongoing|active|working\s+on)\b",
    re.IGNORECASE,
)
_COMPLETED_PATTERN = re.compile(
    r"\b(completed|complete|done|finished|closed|resolved)\b",
    re.IGNORECASE,
)
_PENDING_PATTERN = re.compile(
    r"\b(pending|not\s+(?:yet\s+)?started|haven'?t\s+started|"
    r"hasn'?t\s+started|to\s+do|todo|yet\s+to\s+start|backlog)\b",
    re.IGNORECASE,
)
_NOT_STARTED_PATTERN = re.compile(
    r"\b(not\s+(?:yet\s+)?started|haven'?t\s+started|hasn'?t\s+started|"
    r"yet\s+to\s+start)\b",
    re.IGNORECASE,
)
_NEGATED_COMPLETED_PATTERN = re.compile(
    r"\b(?:not|never|isn'?t|aren'?t|hasn'?t|haven'?t|wasn'?t|weren'?t)\b"
    r"[^.?!]{0,20}\b(?:completed|complete|done|finished|closed)\b",
    re.IGNORECASE,
)
_EXPLICIT_STATUS_PATTERN = re.compile(r"\bstatus\b", re.IGNORECASE)


class ReadPlanError(ValueError):
    """Raised when a read plan is not valid for the schema."""


def schema_text() -> str:
    """A compact, model-readable description of the live schema."""

    lines = ["table tasks ("]

    for column in Task.__table__.columns:
        nullable = "NULL" if column.nullable else "NOT NULL"
        default = ""

        if (
            column.default is not None
            and getattr(column.default, "arg", None) is not None
        ):
            default = f" DEFAULT {column.default.arg}"

        lines.append(f"  {column.name} {column.type} {nullable}{default}")

    lines.append(")")
    lines.append(
        "status is one of PENDING, IN_PROGRESS, COMPLETED; progress is 0-100."
    )
    return "\n".join(lines)


def _normalize(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def statuses_from_text(text: str | None) -> tuple[str, ...]:
    """The status scope of a natural-language message.

    ``"pending tasks"`` means the open work (PENDING and IN_PROGRESS), which
    is how people normally use the word. ``"status is pending"`` and
    ``"only last pending"`` mean the exact PENDING value. ``"completed"`` and
    ``"in progress"`` keep their exact meaning.
    """

    normalized = _normalize(text)

    if not normalized:
        return ()

    # "not completed" is open work even though it contains "completed".
    if _NEGATED_COMPLETED_PATTERN.search(normalized):
        return OPEN_STATUSES

    if _COMPLETED_PATTERN.search(normalized):
        return ("COMPLETED",)

    if _IN_PROGRESS_PATTERN.search(normalized) and not _PENDING_PATTERN.search(
        normalized
    ):
        return ("IN_PROGRESS",)

    if _NOT_STARTED_PATTERN.search(normalized):
        return ("PENDING",)

    if _PENDING_PATTERN.search(normalized):
        if _EXPLICIT_STATUS_PATTERN.search(normalized):
            return ("PENDING",)
        return OPEN_STATUSES

    if _OPEN_PATTERN.search(normalized):
        return OPEN_STATUSES

    return ()


def resolve_statuses(
    text: str | None, extracted_status: str | None
) -> tuple[str, ...]:
    """Combine the message wording with the model-extracted status."""

    detected = statuses_from_text(text)

    if detected:
        return detected

    if extracted_status:
        value = extracted_status.strip().upper()

        if value in STATUS_VALUES:
            return (value,)

    return ()


def _meaningful_tokens(text: str | None) -> list[str]:
    concepts = task_search.query_concepts(text)
    result = []

    for concept in concepts:
        if concept in _COMMAND_WORDS or concept in _STATUS_WORDS:
            continue
        if concept in _COMMAND_STEMS or concept in _STATUS_STEMS:
            continue
        if len(concept) < 2 and not concept.isdigit():
            continue
        if concept not in result:
            result.append(concept)

    return result


_TITLE_MARKER_PATTERN = re.compile(
    r"\b(?:about|regarding|related\s+to|called|named|titled|"
    r"with\s+(?:the\s+)?title|for\s+(?:the\s+)?task)\s+"
    r"(?:[\"']([^\"']+)[\"']|([^\"']+?))"
    r"(?=\s*(?:[?.!]|$|\bassigned\b|\bfor\b|\bwith\b|\bfrom\b))",
    re.IGNORECASE,
)


def _explicit_title_phrase(text: str | None) -> str | None:
    """A title named after about/called/titled or inside quotes."""

    value = (text or "").strip()

    if not value:
        return None

    match = _TITLE_MARKER_PATTERN.search(value)

    if match:
        return (match.group(1) or match.group(2) or "").strip() or None

    return None


def _without_excluded(tokens: list[str], exclude) -> list[str]:
    excluded = set()

    for value in exclude or ():
        excluded.update(task_search.query_concepts(value))

    if not excluded:
        return tokens

    return [token for token in tokens if token not in excluded]


def clean_title_filter(
    text: str | None,
    extracted_title: str | None,
    *,
    exclude=(),
) -> str | None:
    """Keep a real title from the request, drop generic/status phrases.

    A model sometimes returns the whole request ("gather all pending tasks")
    as the title filter. That can never match a stored title, so the model
    value is only used when it is a distinct phrase. Otherwise the title is
    taken from an explicit marker ("tasks about MCP", "the task called ...").
    """

    message_norm = _normalize(text)
    title_norm = _normalize(extracted_title)

    if title_norm and title_norm != message_norm:
        meaningful = _without_excluded(
            _meaningful_tokens(extracted_title), exclude
        )

        if meaningful:
            return " ".join(meaningful)

    meaningful = _without_excluded(
        _meaningful_tokens(_explicit_title_phrase(text)), exclude
    )

    if meaningful:
        return " ".join(meaningful)

    return None


def _error(message: str) -> ReadPlanError:
    return ReadPlanError(message)


@dataclass(frozen=True)
class ReadFilter:
    column: str
    operator: str = "eq"
    value: Any = None


@dataclass(frozen=True)
class ReadOrder:
    column: str
    direction: str = "asc"


@dataclass
class ReadPlan:
    columns: tuple[str, ...] = field(default_factory=lambda: MODEL_COLUMNS)
    filters: tuple[ReadFilter, ...] = ()
    order_by: tuple[ReadOrder, ...] = (ReadOrder("id", "asc"),)
    limit: int = DEFAULT_LIMIT
    requested_field: str | None = None
    explanation: str | None = None

    def as_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "filters": [
                {
                    "column": item.column,
                    "operator": item.operator,
                    "value": item.value,
                }
                for item in self.filters
            ],
            "order_by": [
                {"column": item.column, "direction": item.direction}
                for item in self.order_by
            ],
            "limit": self.limit,
            "requested_field": self.requested_field,
            "explanation": self.explanation,
        }


def plan_from_dict(data: dict) -> ReadPlan:
    """Build and validate a plan from JSON-shaped data."""

    if not isinstance(data, dict):
        raise _error("The plan must be a JSON object.")

    columns = data.get("columns") or list(MODEL_COLUMNS)

    if isinstance(columns, str):
        columns = [columns]

    if not isinstance(columns, (list, tuple)) or not columns:
        raise _error("The plan must select at least one column.")

    filters = []

    raw_filters = data.get("filters") or []

    if isinstance(raw_filters, dict):
        raw_filters = [raw_filters]

    if not isinstance(raw_filters, (list, tuple)):
        raise _error("filters must be a list.")

    for item in raw_filters:
        if not isinstance(item, dict):
            raise _error("Each filter must be an object.")
        filters.append(
            ReadFilter(
                column=str(item.get("column") or ""),
                operator=str(item.get("operator") or "eq"),
                value=item.get("value"),
            )
        )

    order_by = []

    raw_order = data.get("order_by") or []

    if isinstance(raw_order, dict):
        raw_order = [raw_order]

    if not isinstance(raw_order, (list, tuple)):
        raise _error("order_by must be a list.")

    for item in raw_order:
        if isinstance(item, str):
            order_by.append(ReadOrder(item, "asc"))
        elif isinstance(item, dict):
            order_by.append(
                ReadOrder(
                    str(item.get("column") or ""),
                    str(item.get("direction") or "asc"),
                )
            )
        else:
            raise _error("Each order_by entry must be a column or an object.")

    limit = data.get("limit", DEFAULT_LIMIT)

    plan = ReadPlan(
        columns=tuple(columns),
        filters=tuple(filters),
        order_by=tuple(order_by or [ReadOrder("id", "asc")]),
        limit=limit,
        requested_field=data.get("requested_field"),
        explanation=data.get("explanation"),
    )

    validate_plan(plan)
    return plan


def _coerce_value(column: str, value):
    if value is None:
        return None

    if column == "status":
        if not isinstance(value, str) or value.upper() not in STATUS_VALUES:
            raise _error("status must be PENDING, IN_PROGRESS, or COMPLETED.")
        return value.upper()

    if column == "progress":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _error("progress must be an integer between 0 and 100.")
        if not 0 <= value <= 100:
            raise _error("progress must be between 0 and 100.")
        return value

    if column == "id":
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _error("id must be a positive integer.")
        return value

    if column in DATE_COLUMNS:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as error:
            raise _error(f"{column} must be a YYYY-MM-DD date.") from error

    if column in TIME_COLUMNS:
        if isinstance(value, time):
            return value
        try:
            parts = str(value).split(":")
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError) as error:
            raise _error(f"{column} must be an HH:MM time.") from error

    if not isinstance(value, str):
        raise _error(f"{column} must be text.")

    return value


def validate_filter(item: ReadFilter) -> ReadFilter:
    column = (item.column or "").strip()
    operator = (item.operator or "eq").strip().lower()

    if column not in READABLE_COLUMNS:
        raise _error(f"Unknown column '{column}'.")

    if operator not in ALLOWED_OPERATORS:
        raise _error(f"Unsupported operator '{operator}'.")

    if operator in _TEXT_OPERATORS and column not in TEXT_COLUMNS:
        raise _error(f"Operator '{operator}' is only valid on text columns.")

    if operator in _RANGE_OPERATORS and column not in (
        INTEGER_COLUMNS | DATE_COLUMNS | TIME_COLUMNS
    ):
        raise _error(f"Operator '{operator}' needs a numeric/date/time column.")

    if operator in _NULL_OPERATORS:
        return ReadFilter(column, operator, None)

    if operator == "in":
        values = item.value

        if not isinstance(values, (list, tuple)) or not values:
            raise _error("The 'in' operator needs a non-empty list.")

        return ReadFilter(
            column,
            operator,
            tuple(_coerce_value(column, value) for value in values),
        )

    value = _coerce_value(column, item.value)

    if value is None:
        raise _error(f"A value is required for '{column}'.")

    return ReadFilter(column, operator, value)


def validate_plan(plan: ReadPlan) -> ReadPlan:
    """Validate a plan against the schema; raises ``ReadPlanError``."""

    if not isinstance(plan, ReadPlan):
        raise _error("The plan must be a ReadPlan.")

    columns = tuple((plan.columns or MODEL_COLUMNS))

    if not columns:
        raise _error("The plan must select at least one column.")

    for column in columns:
        if column not in READABLE_COLUMNS:
            raise _error(f"Unknown column '{column}'.")

    validated_filters = tuple(validate_filter(item) for item in plan.filters)

    for order in plan.order_by:
        if order.column not in READABLE_COLUMNS:
            raise _error(f"Unknown order column '{order.column}'.")
        if order.direction.lower() not in {"asc", "desc"}:
            raise _error("Order direction must be 'asc' or 'desc'.")

    if isinstance(plan.limit, bool) or not isinstance(plan.limit, int):
        raise _error("limit must be an integer.")
    if not 1 <= plan.limit <= MAX_LIMIT:
        raise _error(f"limit must be between 1 and {MAX_LIMIT}.")

    plan.columns = columns
    plan.filters = validated_filters
    plan.order_by = tuple(
        ReadOrder(item.column, item.direction.lower()) for item in plan.order_by
    )

    return plan


def _condition(item: ReadFilter, index: int) -> tuple[str, dict]:
    column = item.column
    operator = item.operator
    params: dict = {}

    if operator == "is_null":
        return f"{column} IS NULL", params

    if operator == "not_null":
        return f"{column} IS NOT NULL", params

    if operator == "in":
        keys = []
        for offset, value in enumerate(item.value):
            key = f"f{index}_{offset}"
            keys.append(f":{key}")
            params[key] = value
        return f"{column} IN ({', '.join(keys)})", params

    if operator == "contains" and column == "task_title":
        terms = task_search.sql_terms(item.value) or [item.value]
        likes = []
        for offset, term in enumerate(terms):
            key = f"f{index}_{offset}"
            likes.append(f"{column} ILIKE :{key}")
            params[key] = f"%{term}%"
        return "(" + " OR ".join(likes) + ")", params

    if operator == "contains":
        key = f"f{index}"
        params[key] = f"%{item.value}%"
        return f"{column} ILIKE :{key}", params

    if operator == "starts_with":
        key = f"f{index}"
        params[key] = f"{item.value}%"
        return f"{column} ILIKE :{key}", params

    key = f"f{index}"
    params[key] = item.value
    symbols = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }
    return f"{column} {symbols[operator]} :{key}", params


def compile_plan(
    plan: ReadPlan, *, owner: str | None = None, intent: str = "LIST_TASKS"
) -> dict:
    """Compile a validated plan into a parameterized SELECT operation."""

    validate_plan(plan)

    params: dict = {}
    conditions: list[str] = []

    for index, item in enumerate(plan.filters):
        condition, values = _condition(item, index)
        conditions.append(condition)
        params.update(values)

    if owner:
        conditions.append("assigned_to ILIKE :owner")
        params["owner"] = owner.strip()

    sql = (
        "SELECT "
        + ", ".join(plan.columns)
        + " FROM tasks"
    )

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    order = ", ".join(
        f"{item.column} {item.direction.upper()}" for item in plan.order_by
    )
    sql += f" ORDER BY {order}"

    sql += f" LIMIT {int(plan.limit)}"

    return {
        "intent": intent,
        "operation": "SELECT",
        "table": TASK_TABLE,
        "columns": list(plan.columns),
        "plan": plan.as_dict(),
        "owner": owner,
        "sql": sql,
        "params": params,
    }


def plan_from_query(
    message: str,
    *,
    status: str | None = None,
    assigned_to: str | None = None,
    title: str | None = None,
    task_id: int | None = None,
    owner: str | None = None,
    intent: str = "LIST_TASKS",
) -> dict:
    """Build the deterministic fallback plan from extracted query fields.

    This is the safety net when the model returns an empty or unusable query.
    It applies the same status semantics as the rest of the module.
    """

    filters = []

    if task_id is not None:
        filters.append(ReadFilter("id", "eq", task_id))

    statuses = resolve_statuses(message, status)
    if len(statuses) == 1:
        filters.append(ReadFilter("status", "eq", statuses[0]))
    elif statuses:
        filters.append(ReadFilter("status", "in", list(statuses)))

    if assigned_to:
        filters.append(ReadFilter("assigned_to", "contains", assigned_to))

    cleaned_title = clean_title_filter(
        message, title, exclude=(assigned_to,)
    )
    if cleaned_title:
        filters.append(ReadFilter("task_title", "contains", cleaned_title))

    plan = ReadPlan(filters=tuple(filters))
    return compile_plan(plan, owner=owner, intent=intent)
