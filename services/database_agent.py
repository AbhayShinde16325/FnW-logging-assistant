"""LLM-controlled, tool-calling database agent.

The model is the reasoning engine. It receives the live schema, the employee
scope, the conversation context and a small set of tools. It decides which
tool to call, inspects the result, and either calls another tool or writes the
final answer. The Python code here only:

* defines the tool contracts,
* dispatches the selected tool,
* enforces authentication/authorization, SQL validation, read-only execution
  and confirmation before any write,
* bounds the loop and records a compact trace for the conversation memory.

It does not route the message with hardcoded intent keywords. The existing
deterministic flows remain available as a fallback when the agent is disabled
or cannot start.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import config
from ai.tool_calling import (
    AssistantTurn,
    ProviderError,
    ToolCall,
    ToolCallingClient,
    classify_provider_error,
    default_client,
    default_message_adapter,
    provider_error_message,
)
from database import read_planner
from database import task_fields
from database import task_repository
from database.models import Task
from database.sql_execution import (
    confirmation_phrase,
    validate_confirmation,
    validate_sql,
)
from services.conversation_memory import (
    get_conversation_memory,
    save_conversation_memory,
    update_active_task,
)
from services.task_scope import current_actor

logger = logging.getLogger(__name__)

TIMEZONE = getattr(config, "TIMEZONE", "Asia/Kolkata")

MAX_ITERATIONS = 6
MAX_READ_ROWS = 200
READ_TIMEOUT_MS = 5000
MAX_TRACE_ENTRIES = 8

OPEN_STATUSES = ("PENDING", "IN_PROGRESS")


def _log_event(event: str, **fields) -> None:
    """Write one structured agent event without logging user content."""

    clean = {key: value for key, value in fields.items() if value is not None}
    details = " ".join(f"{key}={clean[key]}" for key in sorted(clean))
    logger.info("%s %s", event, details, extra={"event": event, **clean})


def _log_provider_error(error: ProviderError, *, conversation_id=None, iteration=None):
    """Log safe provider details, including the provider trace id."""

    logger.warning(
        "Provider error: category=%s status=%s trace_id=%s detail=%s",
        error.category,
        error.status_code,
        error.trace_id,
        error.provider_detail,
        extra={
            "event": "provider_error",
            "conversation_id": conversation_id,
            "iteration": iteration,
            "error_category": error.category,
            "provider_status": error.status_code,
            "provider_trace_id": error.trace_id,
        },
    )


# --- tool contracts ---------------------------------------------------------


def _tool(name: str, description: str, properties: dict, required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


TOOL_DEFINITIONS = [
    _tool(
        "inspect_database_schema",
        (
            "Return the authoritative schema of the only permitted table "
            "(tasks), its value rules and the current date. Call this when the "
            "schema or a column is not already clear."
        ),
        {},
    ),
    _tool(
        "inspect_table_definition",
        "Return the columns, types, nullability and defaults of one table.",
        {
            "table": {
                "type": "string",
                "description": "Table name. The only permitted value is tasks.",
            }
        },
        ("table",),
    ),
    _tool(
        "validate_sql_query",
        (
            "Validate one SQL statement against the security policy without "
            "executing it. Returns the operation, whether confirmation is "
            "required, and a reason when the statement is rejected."
        ),
        {
            "sql": {"type": "string", "description": "One SQL statement."},
            "params": {
                "type": "object",
                "description": "Named parameters for :placeholders in sql.",
            },
        },
        ("sql",),
    ),
    _tool(
        "execute_read_query",
        (
            "Execute one validated SELECT against the tasks table and return "
            "the rows. The transaction is read-only, bounded by a timeout and "
            "a row limit. Always include assigned_to in the selected columns "
            "when an employee scope is active."
        ),
        {
            "sql": {"type": "string", "description": "One SELECT statement."},
            "params": {
                "type": "object",
                "description": "Named parameters for :placeholders in sql.",
            },
            "max_rows": {
                "type": "integer",
                "description": f"Maximum rows to return (1-{MAX_READ_ROWS}).",
            },
        },
        ("sql",),
    ),
    _tool(
        "execute_write_query",
        (
            "Execute one validated INSERT, UPDATE, DELETE or DROP TABLE tasks. "
            "UPDATE, DELETE and DROP require the exact confirmation phrase from "
            "the employee; the runtime supplies it, never the model. Employees "
            "may create tasks only for themselves and may update or delete "
            "only their own tasks. Use named parameters and target updates and "
            "deletes by id; never change task assignment."
        ),
        {
            "sql": {"type": "string", "description": "One write statement."},
            "params": {
                "type": "object",
                "description": "Named parameters for :placeholders in sql.",
            },
        },
        ("sql",),
    ),
    _tool(
        "retrieve_conversation_context",
        (
            "Return the stored conversation context: active task, displayed "
            "task candidates, recent turns and the previous agent trace. Also "
            "re-reads the active task from PostgreSQL so live values are fresh."
        ),
        {},
    ),
    _tool(
        "update_conversation_context",
        (
            "Remember a task reference or a short note in the conversation "
            "memory. The task id is checked against PostgreSQL and the employee "
            "ownership before it is stored."
        ),
        {
            "active_task_id": {
                "type": "integer",
                "description": "Task the user is clearly referring to.",
            },
            "note": {
                "type": "string",
                "description": "Short note, at most 300 characters.",
            },
        },
    ),
]


# --- context and result helpers ---------------------------------------------


@dataclass
class ToolContext:
    message: str
    employee: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    confirmation: str | None = None
    memory: dict | None = None
    notification_context: dict = field(default_factory=dict)
    max_rows: int = MAX_READ_ROWS
    timeout_ms: int = READ_TIMEOUT_MS
    read_executor: Any = None
    write_executor: Any = None
    tool_trace: list = field(default_factory=list)

    @property
    def identity(self) -> str | None:
        return (self.user_id or self.employee or "").strip() or None

    @property
    def conversation(self) -> str | None:
        return (self.conversation_id or "").strip() or None


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]

    return value


def _table_definition() -> dict:
    columns = []

    for column in Task.__table__.columns:
        default = None
        if getattr(column.default, "arg", None) is not None:
            default = column.default.arg

        columns.append(
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": bool(column.nullable),
                "primary_key": bool(column.primary_key),
                "default": _jsonable(default),
            }
        )

    return {
        "status": "success",
        "table": "tasks",
        "columns": columns,
        "status_values": ["PENDING", "IN_PROGRESS", "COMPLETED"],
        "progress_range": [0, 100],
        "notes": [
            "status COMPLETED requires progress 100",
            "progress > 0 must be IN_PROGRESS or COMPLETED",
            "reminder must be after now and at or before the deadline",
            "NULL means not stated",
        ],
    }


def _schema_tool() -> dict:
    today = datetime.now(ZoneInfo(TIMEZONE))

    return {
        "status": "success",
        "schema": read_planner.schema_text(),
        "table": "tasks",
        "today": today.date().isoformat(),
        "now": today.strftime("%Y-%m-%d %H:%M"),
        "timezone": TIMEZONE,
        "guardrails": [
            "only the tasks table may be referenced",
            "one statement per tool call",
            "parameterized values only",
            "SELECT and INSERT do not require confirmation",
            "UPDATE, DELETE and DROP TABLE tasks require their exact phrase",
            "DELETE needs a specific WHERE clause",
        ],
    }


def _authorize_rows(rows: list, employee: str | None) -> tuple[list, str | None]:
    actor = current_actor()

    if actor is not None:
        if actor.has_permission("tasks.read_all"):
            return rows, None

        allowed = []

        for row in rows:
            task_id = row.get("id")

            if task_id is not None:
                task = task_repository.get_task_for_owner(
                    int(task_id), actor.name
                )

                if task is not None:
                    allowed.append(row)
                continue

            if "assigned_to_employee_id" not in row:
                return [], (
                    "The selected columns must include "
                    "assigned_to_employee_id so the runtime can enforce the "
                    "employee scope."
                )

            try:
                owned = int(row.get("assigned_to_employee_id")) == actor.id
            except (TypeError, ValueError):
                owned = False

            if owned:
                allowed.append(row)

        return allowed, None

    if not employee:
        return rows, None

    owner = employee.strip().lower()

    for row in rows:
        if "assigned_to" not in row:
            return [], (
                "The selected columns must include assigned_to so the runtime "
                "can enforce the employee scope."
            )

    return [
        row
        for row in rows
        if str(row.get("assigned_to") or "").strip().lower() == owner
    ], None


def _has_ownership_predicate(sql: str, params: dict, employee: str) -> bool:
    if "assigned_to" not in (sql or "").lower():
        return False

    owner = employee.strip().lower()

    for value in (params or {}).values():
        if isinstance(value, str) and value.strip().lower() == owner:
            return True

    return False


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())

    return None


EMPLOYEE_INSERT_COLUMNS = frozenset(
    {
        "task_title",
        "task_description",
        "assigned_to",
        "assigned_to_employee_id",
        "assigned_date",
        "assigned_time",
        "deadline_date",
        "deadline_time",
        "reminder_date",
        "reminder_time",
        "status",
        "progress",
    }
)
EMPLOYEE_UPDATE_COLUMNS = EMPLOYEE_INSERT_COLUMNS - {
    "assigned_to",
    "assigned_to_employee_id",
}
DATE_COLUMNS = frozenset(
    {"assigned_date", "deadline_date", "reminder_date"}
)
TIME_COLUMNS = frozenset({"assigned_time", "deadline_time", "reminder_time"})
ASSIGNMENT_COLUMNS = frozenset({"assigned_to", "assigned_to_employee_id"})


def _normalise_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").replace('"', "")).strip().rstrip(";")


def _split_csv(value: str) -> list[str]:
    parts = []
    depth = 0
    current = []

    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue

        current.append(character)

    parts.append("".join(current).strip())
    return parts


def _coerce_column_value(column: str, value):
    if value is None:
        return None

    if column in DATE_COLUMNS and not isinstance(value, date):
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ValueError(f"{column} must be an ISO date.")

    if column in TIME_COLUMNS and not isinstance(value, time):
        text = str(value)

        try:
            return time.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{column} must be an ISO time.")

    if column == "assigned_to_employee_id":
        parsed = _as_int(value)
        if parsed is None:
            raise ValueError("assigned_to_employee_id must be an integer.")
        return parsed

    if column == "progress":
        parsed = _as_int(value)
        if parsed is None:
            raise ValueError("progress must be an integer from 0 to 100.")
        return parsed

    return value


def _params_for_columns(columns: list[str], binds: list[str], params: dict):
    values = {}

    for column, bind in zip(columns, binds):
        if bind is None:
            return None, "Write values must be named parameters."

        if bind not in (params or {}):
            return None, f"The parameter :{bind} is missing."

        try:
            values[column] = _coerce_column_value(column, params[bind])
        except ValueError as error:
            return None, str(error)

    return values, None


def _where_task_id(where_clause: str, params: dict):
    conditions = re.split(r"\s+AND\s+", where_clause, flags=re.IGNORECASE)
    task_bind = None

    for condition in conditions:
        task_match = re.fullmatch(
            r"(?:tasks\.)?id\s*=\s*:([A-Za-z_]\w*)",
            condition.strip(),
            re.IGNORECASE,
        )
        owner_match = re.fullmatch(
            r"(?:tasks\.)?assigned_to_employee_id\s*=\s*:([A-Za-z_]\w*)",
            condition.strip(),
            re.IGNORECASE,
        )

        if task_match is not None and task_bind is None:
            task_bind = task_match.group(1)
        elif owner_match is not None:
            continue
        else:
            return None, "Employees must target one task by its id."

    if task_bind is None:
        return None, "A valid task id parameter is required."

    task_id = _as_int((params or {}).get(task_bind))

    if task_id is None or task_id <= 0:
        return None, "A valid task id parameter is required."

    return task_id, None


def _employee_insert(sql: str, params: dict, actor) -> dict:
    statement = _normalise_sql(sql)
    match = re.fullmatch(
        r"INSERT\s+INTO\s+(?:public\.)?tasks\s*\(([^)]*)\)\s*"
        r"VALUES\s*\(([^)]*)\)(?:\s+RETURNING\s+.*)?",
        statement,
        re.IGNORECASE,
    )

    if match is None:
        return {
            "status": "error",
            "operation": "INSERT",
            "message": (
                "Use INSERT INTO tasks (columns) VALUES (:parameters) with "
                "named parameters only."
            ),
        }

    columns = [item.strip().lower() for item in match.group(1).split(",")]
    value_parts = _split_csv(match.group(2))

    if len(columns) != len(value_parts):
        return {
            "status": "error",
            "operation": "INSERT",
            "message": "Every inserted column needs one parameter.",
        }

    unknown = set(columns) - EMPLOYEE_INSERT_COLUMNS

    if unknown:
        return {
            "status": "error",
            "operation": "INSERT",
            "message": "Only task fields can be inserted.",
        }

    binds = []

    for part in value_parts:
        match_bind = re.fullmatch(
            r":([A-Za-z_]\w*)",
            part.strip(),
            re.IGNORECASE,
        )

        if match_bind is None:
            return {
                "status": "error",
                "operation": "INSERT",
                "message": "Insert values must be named parameters.",
            }

        binds.append(match_bind.group(1))

    values, error = _params_for_columns(columns, binds, params)

    if error:
        return {"status": "error", "operation": "INSERT", "message": error}

    from services import task_management

    try:
        task = task_management.create_task(values)
    except task_management.TaskPermissionError as error:
        return {"status": "error", "operation": "INSERT", "message": str(error)}
    except task_management.TaskManagementError as error:
        return {"status": "error", "operation": "INSERT", "message": str(error)}

    return {
        "status": "success",
        "operation": "INSERT",
        "task_id": task.id,
        "task": _jsonable(
            {
                "id": task.id,
                "task_title": task.task_title,
                "assigned_to": task.assigned_to,
                "assigned_to_employee_id": task.assigned_to_employee_id,
                "status": task.status,
                "progress": task.progress,
            }
        ),
        "sql": sql,
    }


def _employee_update(sql: str, params: dict, actor) -> dict:
    statement = _normalise_sql(sql)
    match = re.fullmatch(
        r"UPDATE\s+(?:public\.)?tasks\s+SET\s+(.*?)\s+WHERE\s+(.*)",
        statement,
        re.IGNORECASE,
    )

    if match is None:
        return {
            "status": "error",
            "operation": "UPDATE",
            "message": "Use UPDATE tasks SET column = :parameter WHERE id = :id.",
        }

    columns = []
    binds = []

    for assignment in _split_csv(match.group(1)):
        match_assignment = re.fullmatch(
            r"(?:tasks\.)?([A-Za-z_]\w*)\s*=\s*:([A-Za-z_]\w*)",
            assignment.strip(),
            re.IGNORECASE,
        )

        if match_assignment is None:
            return {
                "status": "error",
                "operation": "UPDATE",
                "message": "Update values must be named parameters.",
            }

        columns.append(match_assignment.group(1).lower())
        binds.append(match_assignment.group(2))

    if set(columns) & ASSIGNMENT_COLUMNS:
        return {
            "status": "error",
            "operation": "UPDATE",
            "message": "You cannot change task assignment.",
        }

    unknown = set(columns) - EMPLOYEE_UPDATE_COLUMNS

    if unknown:
        return {
            "status": "error",
            "operation": "UPDATE",
            "message": "Only task fields can be updated.",
        }

    task_id, error = _where_task_id(match.group(2), params)

    if error:
        return {"status": "error", "operation": "UPDATE", "message": error}

    values, error = _params_for_columns(columns, binds, params)

    if error:
        return {"status": "error", "operation": "UPDATE", "message": error}

    from services import task_management

    try:
        task = task_management.update_task(task_id, values)
    except task_management.TaskNotFoundError:
        return {
            "status": "not_found",
            "operation": "UPDATE",
            "message": "That task is not assigned to you.",
        }
    except task_management.TaskManagementError as error:
        return {"status": "error", "operation": "UPDATE", "message": str(error)}

    return {
        "status": "success",
        "operation": "UPDATE",
        "task_id": task_id,
        "task": _jsonable(
            {
                "id": task.id,
                "status": task.status,
                "progress": task.progress,
                "assigned_to_employee_id": task.assigned_to_employee_id,
            }
        ),
        "sql": sql,
    }


def _employee_delete(sql: str, params: dict, actor) -> dict:
    statement = _normalise_sql(sql)
    match = re.fullmatch(
        r"DELETE\s+FROM\s+(?:public\.)?tasks\s+WHERE\s+(.*)",
        statement,
        re.IGNORECASE,
    )

    if match is None:
        return {
            "status": "error",
            "operation": "DELETE",
            "message": "Use DELETE FROM tasks WHERE id = :id.",
        }

    task_id, error = _where_task_id(match.group(1), params)

    if error:
        return {"status": "error", "operation": "DELETE", "message": error}

    from services import task_management

    try:
        task_management.delete_task(task_id)
    except task_management.TaskNotFoundError:
        return {
            "status": "not_found",
            "operation": "DELETE",
            "message": "That task is not assigned to you.",
        }
    except task_management.TaskManagementError as error:
        return {"status": "error", "operation": "DELETE", "message": str(error)}

    return {
        "status": "success",
        "operation": "DELETE",
        "task_id": task_id,
        "sql": sql,
    }


def _employee_write(sql: str, params: dict, actor, operation: str) -> dict:
    if operation == "INSERT":
        return _employee_insert(sql, params, actor)
    if operation == "UPDATE":
        return _employee_update(sql, params, actor)
    if operation == "DELETE":
        return _employee_delete(sql, params, actor)

    return {
        "status": "error",
        "operation": operation,
        "message": "Employees cannot perform this database operation.",
    }


def _compact_arguments(name: str, arguments: dict) -> dict:
    compact = {}

    for key, value in (arguments or {}).items():
        if key == "sql" and isinstance(value, str):
            compact[key] = value[:400]
        elif isinstance(value, str):
            compact[key] = value[:200]
        else:
            compact[key] = value

    return compact


def _trace_entry(name: str, arguments: dict, result: dict) -> dict:
    entry = {
        "tool": name,
        "arguments": _compact_arguments(name, arguments),
        "status": result.get("status"),
    }

    for key in ("operation", "row_count", "truncated", "confirmation_phrase"):
        if key in result:
            entry[key] = result[key]

    message = result.get("message") or result.get("reason")
    if message:
        entry["message"] = str(message)[:300]

    return entry


def _intent_from_trace(executed: list, rows: list) -> str:
    operations = [
        result.get("operation")
        for _, result in executed
        if result.get("status") == "success"
    ]

    if "INSERT" in operations:
        return "CREATE_TASK"
    if "DROP TABLE" in operations:
        return "DELETE_TASK"
    if "DELETE" in operations:
        return "DELETE_TASK"
    if "UPDATE" in operations:
        return "UPDATE_TASK"
    if "SELECT" in operations:
        return "GET_TASK" if len(rows) == 1 else "LIST_TASKS"

    return "GENERAL_QUERY"


CONTROL_TOKEN = re.compile(r"<[A-Za-z0-9_]+>")


def _clean_final_text(text: str) -> str:
    """Remove provider control tokens and surrounding whitespace."""

    cleaned = CONTROL_TOKEN.sub("", text or "")
    lines = [line.strip() for line in cleaned.splitlines()]

    return "\n".join(line for line in lines if line).strip()


def _direct_field_answer(ctx: ToolContext, tasks: list):
    """Answer one requested task field directly from live task data."""

    fields = task_fields.resolve_requested_fields(ctx.message)

    if len(fields) != 1:
        return None

    field = fields[0]

    if field == task_fields.FULL_DETAILS:
        return None

    task = tasks[0] if len(tasks) == 1 else None

    if task is None:
        active_task_id = (ctx.memory or {}).get("active_task_id")

        if active_task_id is None:
            return None

        if ctx.employee:
            task = task_repository.get_task_for_owner(
                active_task_id, ctx.employee
            )
        else:
            task = task_repository.get_task(active_task_id)

        if task is None:
            return None

    try:
        value = task_fields.format_field_value(field, task)
    except (TypeError, ValueError):
        return None

    return field, value


def _build_result(
    final_text: str,
    *,
    executed: list,
    trace: list,
    ctx: ToolContext,
    iterations: int,
) -> dict:
    tasks = []

    # The latest successful read rows or written task are the source of truth.
    for _, result in executed:
        if result.get("status") != "success":
            continue

        if isinstance(result.get("rows"), list):
            tasks = [
                row for row in result["rows"] if isinstance(row, dict)
            ]
        elif isinstance(result.get("task"), dict):
            tasks = [result["task"]]
    task = tasks[0] if len(tasks) == 1 else None
    intent = _intent_from_trace(executed, tasks)
    direct = (
        _direct_field_answer(ctx, tasks)
        if (not executed or intent == "GET_TASK")
        else None
    )

    confirmation = None

    for _, result in reversed(executed):
        if result.get("status") == "confirmation_required":
            confirmation = result
            break

    status = "success"

    if confirmation is not None and not any(
        result.get("status") == "success" for _, result in executed
    ):
        status = "confirmation_required"

    if direct is not None:
        final_text = direct[1]
    else:
        final_text = _clean_final_text(final_text)

    if not final_text:
        if confirmation is not None:
            final_text = (
                "This operation requires explicit confirmation. Reply with "
                f"`{confirmation.get('confirmation_phrase')}` to run it."
            )
        elif tasks:
            final_text = "I found the requested task data."
        else:
            final_text = "I could not find a matching task."

    candidates = [
        {
            "id": row.get("id"),
            "task_title": row.get("task_title"),
            "assigned_to": row.get("assigned_to"),
            "status": row.get("status"),
        }
        for row in tasks[:10]
        if row.get("id") is not None
    ]

    result = {
        "status": status,
        "intent": intent,
        "message": final_text,
        "reply": final_text,
        "count": len(tasks),
        "tasks": tasks,
        "task": task,
        "planner": "llm_agent",
        "agent": {
            "iterations": iterations,
            "tools": trace,
        },
        "draft": {"candidates": candidates} if candidates else {},
        "agent_context": {
            "candidates": candidates,
            "trace": trace[-MAX_TRACE_ENTRIES:],
        },
        "filters": {},
    }

    if direct is not None:
        result["requested_field"] = direct[0]
        result["value"] = direct[1]

    if confirmation is not None:
        result["confirmation_phrase"] = confirmation.get("confirmation_phrase")
        result["operation"] = confirmation.get("operation")
        result["sql"] = confirmation.get("sql")
        result["params"] = confirmation.get("params")

    return result


def _build_error_result(
    error: ProviderError,
    *,
    executed: list,
    trace: list,
    ctx: ToolContext,
    iterations: int,
) -> dict:
    """A failed model turn is never reported as a successful agent result."""

    text = provider_error_message(error)
    result = _build_result(
        text,
        executed=executed,
        trace=trace,
        ctx=ctx,
        iterations=iterations,
    )

    result["status"] = "error"
    result["message"] = text
    result["reply"] = text
    result["error_category"] = error.category
    result["agent"]["error_category"] = error.category

    if error.trace_id:
        result["agent"]["provider_trace_id"] = error.trace_id

    return result


# --- tool implementations ---------------------------------------------------


def _read_query(arguments: dict, ctx: ToolContext) -> dict:
    sql = arguments.get("sql")
    params = arguments.get("params") or {}

    if not isinstance(sql, str) or not sql.strip():
        return {"status": "error", "message": "A non-empty SQL string is required."}

    if not isinstance(params, dict):
        return {"status": "error", "message": "params must be an object."}

    validation = validate_sql(sql)

    if not validation.allowed:
        return {
            "status": "error",
            "operation": validation.operation,
            "message": validation.reason,
        }

    if validation.operation != "SELECT":
        return {
            "status": "error",
            "message": "execute_read_query only accepts SELECT statements.",
        }

    actor = current_actor()

    if actor is not None and not actor.has_permission("tasks.read"):
        return {
            "status": "error",
            "message": "You do not have permission to read tasks.",
        }

    max_rows = arguments.get("max_rows", ctx.max_rows)

    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        max_rows = ctx.max_rows

    max_rows = max(1, min(int(max_rows), MAX_READ_ROWS))
    executor = ctx.read_executor or task_repository.execute_read_sql

    try:
        result = executor(
            sql,
            params,
            max_rows=max_rows,
            timeout_ms=ctx.timeout_ms,
        )
    except Exception as error:
        logger.warning("The read query failed: %s", error)
        return {
            "status": "error",
            "operation": "SELECT",
            "message": f"The database rejected the read query: {error}",
        }

    rows = result.get("rows") or []
    scoped, error = _authorize_rows(rows, ctx.employee)

    if error:
        return {"status": "error", "operation": "SELECT", "message": error}

    return {
        "status": "success",
        "operation": "SELECT",
        "rows": _jsonable(scoped),
        "row_count": len(scoped),
        "truncated": bool(result.get("truncated")),
        "sql": sql,
    }


def _write_query(arguments: dict, ctx: ToolContext) -> dict:
    sql = arguments.get("sql")
    params = arguments.get("params") or {}

    if not isinstance(sql, str) or not sql.strip():
        return {"status": "error", "message": "A non-empty SQL string is required."}

    if not isinstance(params, dict):
        return {"status": "error", "message": "params must be an object."}

    validation = validate_sql(sql)

    if not validation.allowed:
        return {
            "status": "error",
            "operation": validation.operation,
            "message": validation.reason,
        }

    if validation.operation == "SELECT":
        return {
            "status": "error",
            "message": "execute_write_query does not accept SELECT statements.",
        }

    actor = current_actor()

    if actor is not None:
        required_permission = {
            "INSERT": "tasks.create",
            "UPDATE": "tasks.update",
            "DELETE": "tasks.delete",
        }.get(validation.operation)

        if required_permission and not actor.has_permission(required_permission):
            return {
                "status": "error",
                "message": "You do not have permission to perform this write.",
            }

    elif ctx.employee and validation.operation in {"UPDATE", "DELETE"}:
        if not _has_ownership_predicate(sql, params, ctx.employee):
            return {
                "status": "error",
                "message": (
                    "An employee-scoped write must restrict the WHERE clause "
                    "with assigned_to and bind the employee value as a "
                    "parameter."
                ),
            }

    if validation.requires_confirmation and not validate_confirmation(
        validation.operation, ctx.confirmation
    ):
        return {
            "status": "confirmation_required",
            "operation": validation.operation,
            "sql": sql,
            "params": params,
            "target_table": validation.table,
            "confirmation_phrase": confirmation_phrase(validation.operation),
            "message": (
                "Explicit confirmation is required before executing this "
                "statement."
            ),
        }

    if actor is not None and not actor.has_permission("tasks.update_all"):
        return _employee_write(sql, params, actor, validation.operation)

    executor = ctx.write_executor or task_repository.execute_write_sql

    try:
        return executor(
            sql,
            params,
            confirmation=ctx.confirmation,
            operation=validation.operation,
        )
    except Exception as error:
        logger.warning("The write query failed: %s", error)
        return {
            "status": "error",
            "operation": validation.operation,
            "message": f"The database rejected the write query: {error}",
        }


def _context_snapshot(ctx: ToolContext) -> dict:
    memory = ctx.memory

    if memory is None and ctx.identity and ctx.conversation:
        memory = get_conversation_memory(ctx.identity, ctx.conversation)

    memory = memory or {}
    context = memory.get("context") or {}

    active_task_id = memory.get("active_task_id")

    if active_task_id is None:
        active_task_id = (context.get("active_task") or {}).get("id")

    actor = current_actor()
    live_task = None

    if active_task_id is not None:
        if actor is not None:
            live_task = task_repository.get_task_for_owner(
                active_task_id, actor.name
            )
        elif ctx.employee:
            live_task = task_repository.get_task_for_owner(
                active_task_id, ctx.employee
            )
        else:
            live_task = task_repository.get_task(active_task_id)

    active_snapshot = context.get("active_task") or {}
    candidates = context.get("candidates") or []

    if actor is not None and not actor.has_permission("tasks.read_all"):
        if live_task is None:
            active_snapshot = {}

        scoped_candidates = []

        for candidate in candidates:
            candidate_id = (candidate or {}).get("id")

            if candidate_id is None:
                continue

            if task_repository.get_task_for_owner(
                candidate_id, actor.name
            ) is not None:
                scoped_candidates.append(candidate)

        candidates = scoped_candidates

    snapshot = {
        "status": "success",
        "active_task_id": active_task_id,
        "active_task": _jsonable(active_snapshot),
        "live_task": _jsonable(
            {
                column.name: getattr(live_task, column.name)
                for column in Task.__table__.columns
            }
        )
        if live_task is not None
        else None,
        "candidates": _jsonable(candidates),
        "history": _jsonable(context.get("history") or []),
        "agent_trace": _jsonable(context.get("agent_trace") or []),
        "last_action": memory.get("last_action"),
    }

    return snapshot


def _retrieve_context(arguments: dict, ctx: ToolContext) -> dict:
    return _context_snapshot(ctx)


def _update_context(arguments: dict, ctx: ToolContext) -> dict:
    if not ctx.identity or not ctx.conversation:
        return {
            "status": "error",
            "message": "A conversation identity is required to store context.",
        }

    active_task_id = arguments.get("active_task_id")
    note = str(arguments.get("note") or "").strip()[:300]

    if active_task_id is not None:
        if isinstance(active_task_id, bool) or not isinstance(active_task_id, int):
            return {"status": "error", "message": "active_task_id must be an integer."}

        memory = update_active_task(
            ctx.identity,
            ctx.conversation,
            active_task_id,
            employee=ctx.employee,
            last_action="DATABASE_AGENT",
            context={"agent_note": note} if note else None,
        )

        if memory is None:
            return {
                "status": "not_found",
                "message": "That task does not exist for this employee.",
            }

        return {"status": "success", "active_task_id": active_task_id}

    save_conversation_memory(
        ctx.identity,
        ctx.conversation,
        context={"agent_note": note},
    )

    return {"status": "success", "active_task_id": None}


TOOL_HANDLERS = {
    "inspect_database_schema": lambda arguments, ctx: _schema_tool(),
    "inspect_table_definition": lambda arguments, ctx: (
        _table_definition()
        if str(arguments.get("table") or "").strip().lower() == "tasks"
        else {
            "status": "error",
            "message": "Only the tasks table may be inspected.",
        }
    ),
    "validate_sql_query": lambda arguments, ctx: _validate_tool(arguments),
    "execute_read_query": _read_query,
    "execute_write_query": _write_query,
    "retrieve_conversation_context": _retrieve_context,
    "update_conversation_context": _update_context,
}


def _validate_tool(arguments: dict) -> dict:
    sql = arguments.get("sql")

    if not isinstance(sql, str) or not sql.strip():
        return {"status": "error", "message": "A non-empty SQL string is required."}

    validation = validate_sql(sql)

    return {
        "status": "success" if validation.allowed else "error",
        "allowed": validation.allowed,
        "operation": validation.operation,
        "table": validation.table,
        "requires_confirmation": validation.requires_confirmation,
        "uses_parameters": validation.uses_parameters,
        "message": validation.reason,
    }


def _tool_parameters(name: str) -> dict | None:
    for tool in TOOL_DEFINITIONS:
        function = tool.get("function") or {}

        if function.get("name") == name:
            return function.get("parameters") or {}

    return None


def _validate_tool_arguments(name: str, arguments: dict) -> str | None:
    """Validate the model's arguments against the published tool schema."""

    if not isinstance(arguments, dict):
        return "Tool arguments must be an object."

    if "_invalid_json" in arguments or "_invalid_arguments" in arguments:
        return "The tool arguments are not valid JSON."

    parameters = _tool_parameters(name)

    if parameters is None:
        return f"Unknown tool '{name}'."

    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or ())
    unknown = set(arguments) - set(properties)

    if unknown:
        return "Unknown tool argument(s): " + ", ".join(sorted(unknown)) + "."

    missing = [
        key
        for key in required
        if key not in arguments or arguments[key] is None
    ]

    if missing:
        return "Missing required tool argument(s): " + ", ".join(sorted(missing)) + "."

    type_checks = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "object": lambda value: isinstance(value, dict),
        "boolean": lambda value: isinstance(value, bool),
    }

    for key, value in arguments.items():
        if value is None:
            continue

        expected = (properties.get(key) or {}).get("type")
        check = type_checks.get(expected)

        if check is not None and not check(value):
            return f"Tool argument '{key}' must be of type {expected}."

    return None


def execute_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    handler = TOOL_HANDLERS.get(name)

    if handler is None:
        return {"status": "error", "message": f"Unknown tool '{name}'."}

    if not isinstance(arguments, dict):
        return {"status": "error", "message": "Tool arguments must be an object."}

    argument_error = _validate_tool_arguments(name, arguments)

    if argument_error:
        return {"status": "error", "message": argument_error}

    try:
        return handler(arguments, ctx)
    except Exception as error:
        logger.exception("The database agent tool '%s' failed.", name)
        return {
            "status": "error",
            "message": f"The tool failed: {error}",
        }


# --- prompt and loop --------------------------------------------------------


def _system_prompt(ctx: ToolContext, model_tools: list | None = None) -> str:
    memory = ctx.memory or {}
    context = memory.get("context") or {}
    active = context.get("active_task") or {}
    candidates = context.get("candidates") or []
    trace = context.get("agent_trace") or []

    actor = current_actor()

    if actor is not None and actor.has_permission("tasks.read_all"):
        scope = (
            f"The signed-in employee is {actor.name}. They may read tasks "
            "across the organisation."
        )
    elif actor is not None:
        scope = (
            f"The signed-in employee is {actor.name} (employee id {actor.id}). "
            "Every read must stay in their rows: select assigned_to and "
            "assigned_to_employee_id and bind an assigned_to_employee_id "
            "predicate to their employee id. They may create tasks only for "
            "themselves, edit their own task fields (not assignment) and "
            "delete their own tasks."
        )
    elif ctx.employee:
        scope = (
            f"The signed-in employee is {ctx.employee}. Every read must stay "
            "in their rows. Select assigned_to and bind the employee value in "
            "an assigned_to ILIKE predicate."
        )
    else:
        scope = "No employee scope was supplied; the request is not restricted."

    notification_ctx = ctx.notification_context or {}
    notification_line = "- notification follow-up context: none"
    if notification_ctx:
        notification_line = (
            "- notification follow-up context: "
            + json.dumps(_jsonable(notification_ctx), default=str)
        )

    return f"""
You are the task database agent of a task-management application. You are the
reasoning engine: decide what the user wants, choose tools, inspect the real
database results and answer from them. Do not follow a fixed script.

Only the `tasks` table may be referenced. The schema is:

{read_planner.schema_text()}

Current time: {datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")} ({TIMEZONE}).
{scope}

Available tools: {", ".join(_tool_names(model_tools or [])) or "none"}.

Conversation context:
- active task snapshot: {json.dumps(_jsonable(active), default=str)}
- displayed candidates: {json.dumps(_jsonable(candidates), default=str)}
{notification_line}
- recent agent trace: {json.dumps(_jsonable(trace[-MAX_TRACE_ENTRIES:]), default=str)}

Rules:
1. Use the tools for all live task data. Never invent a task id, title,
   assignee, date, progress value or status.
2. Generate SQL for the tools. Use named parameters (`:name`) and pass the
   values in `params`; never inline user values. Quote only with single quotes
   for real SQL literals, never for user data.
3. Prefer explicit column lists. Include `assigned_to` in every read when an
   employee scope is active.
4. "pending", "outstanding", "open", "unfinished", "remaining" normally mean
   status in ('PENDING', 'IN_PROGRESS'). The exact PENDING value is used only
   when the user says "status is pending" or clearly refers to the stored
   status.
5. If a query is rejected or returns no rows, inspect the error or result and
   correct the query. Do not repeat an identical tool call.
6. A tool result is the source of truth. Re-fetch live values when the user
   asks for current data; memory may be stale.
7. If several tasks could be the intended one and the user has not identified
   one, ask a short clarification question. Use the candidates and the active
   task from the context.
8. For one requested field, answer with that field only. For example:
   progress -> "40% completed."; status -> "PENDING."; deadline ->
   "25 November 2026." Use DD Month YYYY for dates.
9. If a field is NULL, say it is not set. If no task matches, say so.
10. Writes: task creation needs no confirmation. Task updates and deletes are
    executed only when the employee supplied the exact phrase. If a write tool
    returns confirmation_required, tell the employee the exact phrase and do
    not claim it ran. Never supply or guess the confirmation phrase.
11. Never print raw SQL or JSON to the employee. Answer in short, natural
    language.
12. Stop as soon as the answer is known. Do not call a tool when the context
    already contains the answer.
13. If notification follow-up context is present, it is the authoritative
    existing-task context for this conversation. The task already exists. Do
    not start task creation, do not ask for a task title, and do not answer
    as if the employee is creating a new task. Use the notification task_id as
    the active task unless the employee explicitly asks to create a different
    new task or work on a different task by explicit reference.

Use the native tools. Return a final natural-language answer only when no
further tool call is needed.
""".strip()


def _tool_names(model_tools: list) -> list[str]:
    names = []

    for tool in model_tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = (function or {}).get("name")

        if name:
            names.append(str(name))

    return names


def _history_messages(ctx: ToolContext) -> list[dict]:
    memory = ctx.memory or {}
    context = memory.get("context") or {}
    history = context.get("history") or []
    messages = []

    for turn in history[-10:]:
        if not isinstance(turn, dict):
            continue

        text = str(turn.get("text") or "").strip()
        if not text:
            continue

        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": text})

    return messages


def _max_iterations() -> int:
    raw = os.getenv("DATABASE_AGENT_MAX_STEPS", "").strip()

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_ITERATIONS

    return max(1, min(value, 12))


def _max_read_rows() -> int:
    raw = os.getenv("DATABASE_AGENT_MAX_ROWS", "").strip()

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_READ_ROWS

    return max(1, min(value, MAX_READ_ROWS))


def _timeout_ms() -> int:
    raw = os.getenv("DATABASE_AGENT_SQL_TIMEOUT_MS", "").strip()

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return READ_TIMEOUT_MS

    return max(500, min(value, 30000))


def run_database_agent(
    message: str,
    *,
    employee: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    memory: dict | None = None,
    confirmation: str | None = None,
    notification_context: dict | None = None,
    client: ToolCallingClient | None = None,
    read_executor=None,
    write_executor=None,
    max_iterations: int | None = None,
) -> dict | None:
    """Run the bounded LLM/tool loop for one turn.

    ``None`` means the agent is disabled or could not start. Once the loop has
    started, a provider failure is returned as an error result instead of a
    successful turn. The dynamic LLM tool selection is never replaced by a
    keyword or branch-based workflow.
    """

    if client is None:
        client = default_client()

    if client is None:
        return None

    ctx = ToolContext(
        message=message,
        employee=(employee or "").strip() or None,
        user_id=(user_id or "").strip() or None,
        conversation_id=(conversation_id or "").strip() or None,
        confirmation=confirmation,
        memory=memory,
        notification_context=notification_context or {},
        max_rows=_max_read_rows(),
        timeout_ms=_timeout_ms(),
        read_executor=read_executor,
        write_executor=write_executor,
    )
    adapter = default_message_adapter(client)
    conversation = ctx.conversation

    model_tools = TOOL_DEFINITIONS

    if hasattr(client, "tool_definitions"):
        try:
            model_tools = client.tool_definitions()
        except Exception as error:
            provider_error = classify_provider_error(error)
            logger.warning("MCP tool discovery failed: %s", provider_error)
            ctx.tool_trace = []
            return _build_error_result(
                provider_error,
                executed=[],
                trace=[],
                ctx=ctx,
                iterations=0,
            )

    messages = [{"role": "system", "content": _system_prompt(ctx, model_tools)}]
    messages.extend(_history_messages(ctx))
    messages.append({"role": "user", "content": message})

    executed = []
    trace = []
    seen = set()
    limit = max_iterations or _max_iterations()

    _log_event(
        "agent_turn_started",
        conversation_id=conversation,
        max_iterations=limit,
    )

    for iteration in range(1, limit + 1):
        started = monotonic()

        try:
            turn: AssistantTurn = client.chat(
                messages=messages, tools=model_tools
            )
        except Exception as error:
            provider_error = classify_provider_error(error)
            duration_ms = round((monotonic() - started) * 1000)

            _log_event(
                "model_request",
                conversation_id=conversation,
                iteration=iteration,
                status="error",
                duration_ms=duration_ms,
                error_category=provider_error.category,
            )
            _log_provider_error(
                provider_error,
                conversation_id=conversation,
                iteration=iteration,
            )

            ctx.tool_trace = trace
            result = _build_error_result(
                provider_error,
                executed=executed,
                trace=trace,
                ctx=ctx,
                iterations=iteration,
            )

            _log_event(
                "final_response",
                conversation_id=conversation,
                iteration=iteration,
                status="error",
                final_status=result.get("status"),
                tool_count=len(executed),
                error_category=provider_error.category,
            )

            return result

        _log_event(
            "model_request",
            conversation_id=conversation,
            iteration=iteration,
            status="success",
            duration_ms=round((monotonic() - started) * 1000),
        )

        # Normalize the ids once, so every tool result refers to exactly the
        # assistant call it answers. Provider ids are preserved.
        calls = []

        for index, call in enumerate(turn.tool_calls or [], start=1):
            call_id = str(call.id or "").strip() or (
                f"agent-call-{iteration}-{index}"
            )
            calls.append(
                ToolCall(
                    id=call_id,
                    name=str(call.name or "").strip(),
                    arguments=call.arguments,
                )
            )

        turn.tool_calls = calls

        if not turn.tool_calls:
            ctx.tool_trace = trace
            result = _build_result(
                turn.content.strip(),
                executed=executed,
                trace=trace,
                ctx=ctx,
                iterations=iteration,
            )

            _log_event(
                "final_response",
                conversation_id=conversation,
                iteration=iteration,
                status="success",
                final_status=result.get("status"),
                tool_count=len(executed),
            )

            return result

        # Only the provider-safe fields are serialized. Internal metadata
        # such as Cohere's tool_plan stays out of the request.
        messages.append(adapter.assistant_message(turn))

        for call in turn.tool_calls:
            signature = (
                call.name,
                json.dumps(call.arguments or {}, sort_keys=True, default=str),
            )
            tool_started = monotonic()

            if signature in seen:
                result = {
                    "status": "error",
                    "message": (
                        "This exact tool call was already made. Use the "
                        "observation already returned or answer the user."
                    ),
                }
            else:
                seen.add(signature)

                if hasattr(client, "call_tool"):
                    result = client.call_tool(
                        call.name,
                        call.arguments,
                        trusted_confirmation=ctx.confirmation,
                    )
                else:
                    result = execute_tool(call.name, call.arguments, ctx)

                executed.append((call.name, result))

            duration_ms = round((monotonic() - tool_started) * 1000)

            _log_event(
                "tool_execution",
                conversation_id=conversation,
                iteration=iteration,
                tool=call.name or "unknown",
                duration_ms=duration_ms,
                status=result.get("status"),
            )

            trace.append(_trace_entry(call.name, call.arguments, result))
            ctx.tool_trace = trace

            messages.append(
                adapter.tool_message(call, _jsonable(result))
            )

    ctx.tool_trace = trace

    if not executed:
        _log_event(
            "final_response",
            conversation_id=conversation,
            status="success",
            final_status="not_started",
            tool_count=0,
        )
        return None

    result = _build_result(
        (
            "I could not finish that request within the allowed number of "
            "steps. Please make the request a little more specific."
        ),
        executed=executed,
        trace=trace,
        ctx=ctx,
        iterations=limit,
    )

    _log_event(
        "final_response",
        conversation_id=conversation,
        status="success",
        final_status=result.get("status"),
        tool_count=len(executed),
    )

    return result
