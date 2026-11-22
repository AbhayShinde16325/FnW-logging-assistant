"""Shared schemas, serializers and error helpers for MCP tools."""

from __future__ import annotations

from datetime import date, time
from typing import Any

from database.sql_execution import confirmation_phrase, validate_confirmation
from services.task_management import TaskManagementError
from services.task_scope import TaskActor, current_actor


def object_schema(
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def ok(**values) -> dict[str, Any]:
    return {"status": "success", **values}


def error(message: str, *, status: str = "error") -> dict[str, Any]:
    return {"status": status, "message": message}


def confirmation_required(operation: str) -> dict[str, Any]:
    phrase = confirmation_phrase(operation)
    return {
        "status": "confirmation_required",
        "operation": operation,
        "confirmation_phrase": phrase,
        "message": (
            "This operation requires explicit confirmation. Send the exact "
            f"phrase `{phrase}` in the confirmation argument."
        ),
    }


def require_confirmation(
    operation: str,
    supplied: str | None,
) -> dict[str, Any] | None:
    if validate_confirmation(operation, supplied):
        return None

    return confirmation_required(operation)


def task_error(error_value: Exception) -> dict[str, Any]:
    if isinstance(error_value, TaskManagementError):
        code = getattr(error_value, "status_code", 400)
        status = {
            403: "forbidden",
            404: "not_found",
        }.get(code, "error")
        return error(str(error_value), status=status)

    return error(f"The task operation failed: {error_value}")


def task_payload(task) -> dict[str, Any]:
    if task is None:
        return {}

    def value(name):
        return getattr(task, name, None)

    def date_value(name):
        item = value(name)
        return item.isoformat() if isinstance(item, date) else item

    def time_value(name):
        item = value(name)
        if isinstance(item, time):
            return item.strftime("%H:%M")
        return item

    return {
        "id": value("id"),
        "task_title": value("task_title"),
        "task_description": value("task_description"),
        "assigned_to": value("assigned_to"),
        "assigned_to_employee_id": value("assigned_to_employee_id"),
        "created_by_employee_id": value("created_by_employee_id"),
        "assigned_date": date_value("assigned_date"),
        "assigned_time": time_value("assigned_time"),
        "deadline_date": date_value("deadline_date"),
        "deadline_time": time_value("deadline_time"),
        "reminder_date": date_value("reminder_date"),
        "reminder_time": time_value("reminder_time"),
        "status": value("status"),
        "progress": value("progress"),
        "created_at": date_value("created_at"),
        "updated_at": date_value("updated_at"),
    }


def parse_date(value: Any, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, bool):
        return value

    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD).") from error


def parse_time(value: Any, field: str) -> time:
    if isinstance(value, time):
        return value

    text = str(value).strip()

    if len(text) == 5:
        text = f"{text}:00"

    try:
        return time.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO time (HH:MM).") from error


def actor_or_none() -> TaskActor | None:
    return current_actor()


def date_properties(*, required: bool = False) -> dict[str, dict]:
    return {
        "date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
        "time": {"type": "string", "description": "ISO time (HH:MM)."},
    }
