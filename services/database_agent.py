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
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import config
from ai.tool_calling import (
    AssistantTurn,
    ToolCallingClient,
    default_client,
)
from database import read_planner
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

logger = logging.getLogger(__name__)

TIMEZONE = getattr(config, "TIMEZONE", "Asia/Kolkata")

MAX_ITERATIONS = 6
MAX_READ_ROWS = 200
READ_TIMEOUT_MS = 5000
MAX_TRACE_ENTRIES = 8

OPEN_STATUSES = ("PENDING", "IN_PROGRESS")


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
            "the employee; the runtime supplies it, never the model. For an "
            "employee-scoped UPDATE or DELETE, restrict the WHERE clause with "
            "assigned_to and bind the employee value as a parameter."
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


def _latest_rows(executed: list) -> tuple[list, dict | None]:
    rows = []
    operation = None

    for name, result in executed:
        if result.get("status") != "success":
            continue

        candidate_rows = result.get("rows")
        if isinstance(candidate_rows, list):
            rows = candidate_rows
            operation = result.get("operation")

    return _jsonable(rows), operation


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


def _build_result(
    final_text: str,
    *,
    executed: list,
    trace: list,
    ctx: ToolContext,
    iterations: int,
) -> dict:
    rows, _ = _latest_rows(executed)

    tasks = [row for row in rows if isinstance(row, dict)]
    task = tasks[0] if len(tasks) == 1 else None
    intent = _intent_from_trace(executed, tasks)

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

    if confirmation is not None:
        result["confirmation_phrase"] = confirmation.get("confirmation_phrase")
        result["operation"] = confirmation.get("operation")
        result["sql"] = confirmation.get("sql")
        result["params"] = confirmation.get("params")

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

    if ctx.employee and validation.operation in {"UPDATE", "DELETE"}:
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

    live_task = None

    if active_task_id is not None:
        if ctx.employee:
            live_task = task_repository.get_task_for_owner(
                active_task_id, ctx.employee
            )
        else:
            live_task = task_repository.get_task(active_task_id)

    snapshot = {
        "status": "success",
        "active_task_id": active_task_id,
        "active_task": _jsonable(context.get("active_task") or {}),
        "live_task": _jsonable(
            {
                column.name: getattr(live_task, column.name)
                for column in Task.__table__.columns
            }
        )
        if live_task is not None
        else None,
        "candidates": _jsonable(context.get("candidates") or []),
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


def execute_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    handler = TOOL_HANDLERS.get(name)

    if handler is None:
        return {"status": "error", "message": f"Unknown tool '{name}'."}

    if not isinstance(arguments, dict):
        return {"status": "error", "message": "Tool arguments must be an object."}

    try:
        return handler(arguments, ctx)
    except Exception as error:
        logger.exception("The database agent tool '%s' failed.", name)
        return {
            "status": "error",
            "message": f"The tool failed: {error}",
        }


# --- prompt and loop --------------------------------------------------------


def _system_prompt(ctx: ToolContext) -> str:
    memory = ctx.memory or {}
    context = memory.get("context") or {}
    active = context.get("active_task") or {}
    candidates = context.get("candidates") or []
    trace = context.get("agent_trace") or []

    scope = (
        f"The signed-in employee is {ctx.employee}. Every read must stay in "
        "their rows. Select assigned_to and bind the employee value in an "
        "assigned_to ILIKE predicate."
        if ctx.employee
        else "No employee scope was supplied; the request is not restricted."
    )

    return f"""
You are the task database agent of a task-management application. You are the
reasoning engine: decide what the user wants, choose tools, inspect the real
database results and answer from them. Do not follow a fixed script.

Only the `tasks` table may be referenced. The schema is:

{read_planner.schema_text()}

Current time: {datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")} ({TIMEZONE}).
{scope}

Conversation context:
- active task snapshot: {json.dumps(_jsonable(active), default=str)}
- displayed candidates: {json.dumps(_jsonable(candidates), default=str)}
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
10. Writes: INSERT needs no confirmation. UPDATE, DELETE and DROP TABLE tasks
    are executed only when the employee supplied the exact phrase. If a write
    tool returns confirmation_required, tell the employee the exact phrase and
    do not claim it ran. Never supply or guess the confirmation phrase.
11. Never print raw SQL or JSON to the employee. Answer in short, natural
    language.
12. Stop as soon as the answer is known. Do not call a tool when the context
    already contains the answer.

Use the native tools. Return a final natural-language answer only when no
further tool call is needed.
""".strip()


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
    client: ToolCallingClient | None = None,
    read_executor=None,
    write_executor=None,
    max_iterations: int | None = None,
) -> dict | None:
    """Run the bounded LLM/tool loop for one turn.

    Returns ``None`` when the agent is disabled or cannot start, so the caller
    can keep the deterministic flow. Once a tool has run, a failure is returned
    as a normal error result instead of falling back, because a fallback could
    repeat a write.
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
        max_rows=_max_read_rows(),
        timeout_ms=_timeout_ms(),
        read_executor=read_executor,
        write_executor=write_executor,
    )

    messages = [{"role": "system", "content": _system_prompt(ctx)}]
    messages.extend(_history_messages(ctx))
    messages.append({"role": "user", "content": message})

    executed = []
    trace = []
    seen = set()
    limit = max_iterations or _max_iterations()

    for iteration in range(1, limit + 1):
        try:
            turn: AssistantTurn = client.chat(
                messages=messages, tools=TOOL_DEFINITIONS
            )
        except Exception as error:
            logger.exception("The database agent model call failed.")
            ctx.tool_trace = trace

            if executed:
                return _build_result(
                    f"I could not finish the request: {error}",
                    executed=executed,
                    trace=trace,
                    ctx=ctx,
                    iterations=iteration,
                )

            return None

        if not turn.tool_calls:
            ctx.tool_trace = trace
            return _build_result(
                turn.content.strip(),
                executed=executed,
                trace=trace,
                ctx=ctx,
                iterations=iteration,
            )

        messages.append(turn.as_message())

        for call in turn.tool_calls:
            signature = (
                call.name,
                json.dumps(call.arguments or {}, sort_keys=True, default=str),
            )

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
                result = execute_tool(call.name, call.arguments, ctx)
                executed.append((call.name, result))

            trace.append(_trace_entry(call.name, call.arguments, result))
            ctx.tool_trace = trace

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(_jsonable(result), default=str),
                }
            )

    ctx.tool_trace = trace

    if not executed:
        return None

    return _build_result(
        (
            "I could not finish that request within the allowed number of "
            "steps. Please make the request a little more specific."
        ),
        executed=executed,
        trace=trace,
        ctx=ctx,
        iterations=limit,
    )
