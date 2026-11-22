"""Task-management MCP tools backed by ``services.task_management``."""

from __future__ import annotations

from typing import Any

from auth.permissions import TASKS_CREATE, TASKS_DELETE, TASKS_READ, TASKS_UPDATE
from database import task_repository
from mcp_server.registry.tool_registry import MCPToolSpec
from mcp_server.security.execution_context import MCPExecutionContext
from mcp_server.tools.common import (
    error,
    object_schema,
    ok,
    parse_date,
    parse_time,
    require_confirmation,
    task_error,
    task_payload,
)
from services import task_management


WRITE_PERMISSIONS = frozenset({TASKS_UPDATE})
READ_PERMISSIONS = frozenset({TASKS_READ})

_TASK_WRITE_PROPERTIES = {
    "task_title": {"type": "string"},
    "task_description": {"type": "string"},
    "assigned_to": {"type": "string"},
    "assigned_to_employee_id": {"type": "integer"},
    "assigned_date": {"type": "string", "description": "YYYY-MM-DD"},
    "assigned_time": {"type": "string", "description": "HH:MM"},
    "deadline_date": {"type": "string", "description": "YYYY-MM-DD"},
    "deadline_time": {"type": "string", "description": "HH:MM"},
    "reminder_date": {"type": "string", "description": "YYYY-MM-DD"},
    "reminder_time": {"type": "string", "description": "HH:MM"},
    "status": {
        "type": "string",
        "enum": ["PENDING", "IN_PROGRESS", "COMPLETED"],
    },
    "progress": {"type": "integer", "minimum": 0, "maximum": 100},
    "confirmation": {
        "type": "string",
        "description": "Exact phrase CONFIRM UPDATE, taken from the employee.",
    },
}


def _values(arguments: dict[str, Any]) -> dict[str, Any]:
    values = {
        key: value
        for key, value in arguments.items()
        if key in _TASK_WRITE_PROPERTIES and value is not None
    }
    values.pop("confirmation", None)

    for name in ("assigned_date", "deadline_date", "reminder_date"):
        if name in values:
            values[name] = parse_date(values[name], name)

    for name in ("assigned_time", "deadline_time", "reminder_time"):
        if name in values:
            values[name] = parse_time(values[name], name)

    return values


def _update(
    arguments: dict[str, Any],
    context: MCPExecutionContext,
    values: dict[str, Any],
):
    pending = require_confirmation("UPDATE", arguments.get("confirmation"))

    if pending is not None:
        return pending

    try:
        task = task_management.update_task(int(arguments["task_id"]), values)
    except (TypeError, ValueError) as failure:
        return task_error(failure)
    except task_management.TaskManagementError as failure:
        return task_error(failure)

    return ok(task=task_payload(task))


def create_task(arguments, context):
    try:
        task = task_management.create_task(_values(arguments))
    except task_management.TaskManagementError as failure:
        return task_error(failure)
    except (TypeError, ValueError) as failure:
        return task_error(failure)

    return ok(task=task_payload(task))


def get_task(arguments, context):
    try:
        task = task_management.get_task_for_actor(int(arguments["task_id"]))
    except task_management.TaskManagementError as failure:
        return task_error(failure)
    except (TypeError, ValueError) as failure:
        return task_error(failure)

    return ok(task=task_payload(task))


def list_tasks(arguments, context):
    try:
        rows = task_repository.search_tasks(
            status=arguments.get("status"),
            assigned_to=arguments.get("assigned_to"),
            limit=None,
        )
    except Exception as failure:
        return task_error(failure)

    start = max(0, int(arguments.get("offset") or 0))
    limit = min(200, max(1, int(arguments.get("limit") or 50)))
    selected = rows[start : start + limit]

    return ok(
        count=len(selected),
        total=len(rows),
        rows=[task_payload(row) for row in selected],
        tasks=[task_payload(row) for row in selected],
        offset=start,
        limit=limit,
    )


def search_tasks(arguments, context):
    try:
        rows = task_repository.search_tasks(
            query=arguments.get("query"),
            task_title=arguments.get("task_title"),
            status=arguments.get("status"),
            assigned_to=arguments.get("assigned_to"),
            limit=None,
        )
    except Exception as failure:
        return task_error(failure)

    start = max(0, int(arguments.get("offset") or 0))
    limit = min(200, max(1, int(arguments.get("limit") or 50)))
    selected = rows[start : start + limit]

    return ok(
        count=len(selected),
        total=len(rows),
        rows=[task_payload(row) for row in selected],
        tasks=[task_payload(row) for row in selected],
        offset=start,
        limit=limit,
    )


def update_task(arguments, context):
    try:
        values = _values(arguments)
    except ValueError as failure:
        return error(str(failure))

    if not values:
        return error("Provide at least one field to update.")

    return _update(arguments, context, values)


def update_task_status(arguments, context):
    try:
        values = _values({"status": arguments.get("status")})
    except ValueError as failure:
        return error(str(failure))

    return _update(arguments, context, values)


def update_task_progress(arguments, context):
    return _update(arguments, context, {"progress": arguments.get("progress")})


def update_task_deadline(arguments, context):
    arguments = dict(arguments)
    arguments["task_id"] = arguments.get("task_id")

    try:
        values = _values(
            {
                "deadline_date": arguments.get("deadline_date"),
                "deadline_time": arguments.get("deadline_time"),
            }
        )
    except ValueError as failure:
        return error(str(failure))

    return _update(arguments, context, values)


def update_task_reminder(arguments, context):
    try:
        values = _values(
            {
                "reminder_date": arguments.get("reminder_date"),
                "reminder_time": arguments.get("reminder_time"),
            }
        )
    except ValueError as failure:
        return error(str(failure))

    return _update(arguments, context, values)


def create_task_reminder(arguments, context):
    return update_task_reminder(arguments, context)


def delete_task(arguments, context):
    pending = require_confirmation("DELETE", arguments.get("confirmation"))

    if pending is not None:
        return pending

    try:
        task_id = task_management.delete_task(int(arguments["task_id"]))
    except task_management.TaskManagementError as failure:
        return task_error(failure)
    except (TypeError, ValueError) as failure:
        return task_error(failure)

    return ok(task_id=task_id, deleted=True)


def build_task_tools() -> list[MCPToolSpec]:
    task_id = {"type": "integer", "description": "Task id."}
    confirmation = {
        "type": "string",
        "description": "Exact phrase CONFIRM UPDATE or CONFIRM DELETE.",
    }
    date_fields = {
        key: _TASK_WRITE_PROPERTIES[key]
        for key in (
            "assigned_date",
            "assigned_time",
            "deadline_date",
            "deadline_time",
            "reminder_date",
            "reminder_time",
        )
    }

    return [
        MCPToolSpec(
            name="create_task",
            title="Create a task",
            description=(
                "Create a task. Employees can create tasks only for themselves; "
                "managers and administrators may assign an active employee."
            ),
            input_schema=object_schema(
                {
                    "task_title": {"type": "string"},
                    "task_description": {"type": "string"},
                    "assigned_to": {"type": "string"},
                    "assigned_to_employee_id": {"type": "integer"},
                    **date_fields,
                    "status": _TASK_WRITE_PROPERTIES["status"],
                    "progress": _TASK_WRITE_PROPERTIES["progress"],
                },
                ("task_title",),
            ),
            permissions=frozenset({TASKS_CREATE}),
            handler=create_task,
        ),
        MCPToolSpec(
            name="get_task",
            title="Get a task",
            description="Return one task the authenticated employee may read.",
            input_schema=object_schema({"task_id": task_id}, ("task_id",)),
            permissions=READ_PERMISSIONS,
            handler=get_task,
        ),
        MCPToolSpec(
            name="list_tasks",
            title="List tasks",
            description=(
                "List tasks visible to the authenticated employee, newest first. "
                "Employees see only their own tasks."
            ),
            input_schema=object_schema(
                {
                    "status": _TASK_WRITE_PROPERTIES["status"],
                    "assigned_to": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                }
            ),
            permissions=READ_PERMISSIONS,
            handler=list_tasks,
        ),
        MCPToolSpec(
            name="search_tasks",
            title="Search tasks",
            description=(
                "Search task title, assignee and status within the caller's "
                "authorized scope."
            ),
            input_schema=object_schema(
                {
                    "query": {"type": "string"},
                    "task_title": {"type": "string"},
                    "status": _TASK_WRITE_PROPERTIES["status"],
                    "assigned_to": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                }
            ),
            permissions=READ_PERMISSIONS,
            handler=search_tasks,
        ),
        MCPToolSpec(
            name="update_task",
            title="Update a task",
            description=(
                "Update fields on a task the caller may update. Requires the "
                "employee's exact CONFIRM UPDATE phrase. Employees cannot "
                "reassign tasks."
            ),
            input_schema=object_schema(
                {"task_id": task_id, **_TASK_WRITE_PROPERTIES},
                ("task_id", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=update_task,
        ),
        MCPToolSpec(
            name="update_task_status",
            title="Update task status",
            description="Update a task status. Requires CONFIRM UPDATE.",
            input_schema=object_schema(
                {
                    "task_id": task_id,
                    "status": _TASK_WRITE_PROPERTIES["status"],
                    "confirmation": confirmation,
                },
                ("task_id", "status", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=update_task_status,
        ),
        MCPToolSpec(
            name="update_task_progress",
            title="Update task progress",
            description="Update task progress (0-100). Requires CONFIRM UPDATE.",
            input_schema=object_schema(
                {
                    "task_id": task_id,
                    "progress": _TASK_WRITE_PROPERTIES["progress"],
                    "confirmation": confirmation,
                },
                ("task_id", "progress", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=update_task_progress,
        ),
        MCPToolSpec(
            name="update_task_deadline",
            title="Update task deadline",
            description="Update a task deadline. Requires CONFIRM UPDATE.",
            input_schema=object_schema(
                {
                    "task_id": task_id,
                    "deadline_date": _TASK_WRITE_PROPERTIES["deadline_date"],
                    "deadline_time": _TASK_WRITE_PROPERTIES["deadline_time"],
                    "confirmation": confirmation,
                },
                ("task_id", "deadline_date", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=update_task_deadline,
        ),
        MCPToolSpec(
            name="update_task_reminder",
            title="Update task reminder",
            description=(
                "Set the reminder date/time on a task. Reminder and deadline are "
                "separate fields. Requires CONFIRM UPDATE."
            ),
            input_schema=object_schema(
                {
                    "task_id": task_id,
                    "reminder_date": _TASK_WRITE_PROPERTIES["reminder_date"],
                    "reminder_time": _TASK_WRITE_PROPERTIES["reminder_time"],
                    "confirmation": confirmation,
                },
                ("task_id", "reminder_date", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=update_task_reminder,
        ),
        MCPToolSpec(
            name="create_task_reminder",
            title="Create a task reminder",
            description=(
                "Create or replace the reminder on an existing task using the "
                "existing reminder service. Requires CONFIRM UPDATE."
            ),
            input_schema=object_schema(
                {
                    "task_id": task_id,
                    "reminder_date": _TASK_WRITE_PROPERTIES["reminder_date"],
                    "reminder_time": _TASK_WRITE_PROPERTIES["reminder_time"],
                    "confirmation": confirmation,
                },
                ("task_id", "reminder_date", "confirmation"),
            ),
            permissions=WRITE_PERMISSIONS,
            handler=create_task_reminder,
        ),
        MCPToolSpec(
            name="delete_task",
            title="Delete a task",
            description=(
                "Permanently delete a task the caller may delete. Requires the "
                "employee's exact CONFIRM DELETE phrase."
            ),
            input_schema=object_schema(
                {"task_id": task_id, "confirmation": confirmation},
                ("task_id", "confirmation"),
            ),
            permissions=frozenset({TASKS_DELETE}),
            handler=delete_task,
        ),
    ]
