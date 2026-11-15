"""Reusable capabilities used by the task agent.

Each function is a small, side-effect-free read tool unless its name says
otherwise. They all go through the repository / execution layer; none of them
accepts SQL, a column name or a value from a model.
"""

from __future__ import annotations

from database import task_fields
from database import task_repository
from services.conversation_memory import get_conversation_memory


def conversation_context(user_id, conversation_id) -> dict:
    """The active task and displayed candidates of one conversation."""

    memory = get_conversation_memory(user_id, conversation_id) or {}
    context = memory.get("context") or {}

    active_task_id = memory.get("active_task_id")

    if active_task_id is None:
        snapshot = context.get("active_task") or {}
        active_task_id = snapshot.get("id")

    return {
        "active_task_id": active_task_id,
        "last_action": memory.get("last_action"),
        "candidates": context.get("candidates") or [],
    }


def get_task(task_id: int, *, employee: str | None = None):
    """Read one fresh task row, scoped to the employee when one is known."""

    if employee:
        return task_repository.get_task_for_owner(task_id, employee)

    return task_repository.get_task(task_id)


def get_task_field(task_id: int, field: str | None, *, employee: str | None = None) -> dict:
    """Read one validated property from the database, never from memory."""

    normalized = task_fields.normalize_field(field)

    if normalized is None or normalized == task_fields.FULL_DETAILS:
        return {
            "status": "error",
            "message": "The requested field is not available in the task database.",
        }

    row = task_repository.get_task_field(task_id, normalized, owner=employee)

    if row is None:
        return {"status": "not_found", "task_id": task_id}

    return {
        "status": "success",
        "task_id": task_id,
        "field": normalized,
        "columns": list(task_fields.field_columns(normalized)),
        "value": task_fields.format_field_value(normalized, row),
    }


def get_conversation_context(user_id, conversation_id) -> dict:
    return conversation_context(user_id, conversation_id)
