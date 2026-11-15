"""Agentic, schema-aware read queries.

The "brain" receives the live schema, the employee message and, when a
previous attempt failed, the observation of that attempt. It returns a
structured :class:`~database.read_planner.ReadPlan` (columns, filters, order,
limit), never free-form SQL. The plan is validated and compiled by
``database.read_planner`` into one parameterized ``SELECT``; the shared
execution layer validates the statement again before it runs.

The loop is bounded and every failed attempt is fed back to the brain, so the
agent can repair an unknown column, a bad filter or a query that returned no
rows. When the brain cannot produce a valid plan, the caller keeps its
deterministic fallback.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol

from pydantic import BaseModel, Field

from database import task_repository
from database.read_planner import (
    MODEL_COLUMNS,
    ReadPlan,
    ReadPlanError,
    compile_plan,
    plan_from_dict,
)
from database.sql_generator import validate_operation

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 3


class ReadPlanFilterModel(BaseModel):
    column: str = ""
    operator: str = "eq"
    value: object | None = None


class ReadPlanOrderModel(BaseModel):
    column: str = "id"
    direction: str = "asc"


class ReadPlanModel(BaseModel):
    """The JSON shape the model must return."""

    columns: list[str] = Field(default_factory=lambda: list(MODEL_COLUMNS))
    filters: list[ReadPlanFilterModel] = Field(default_factory=list)
    order_by: list[ReadPlanOrderModel] = Field(
        default_factory=lambda: [ReadPlanOrderModel()]
    )
    limit: int = 100
    requested_field: str | None = None
    explanation: str | None = None


class ReadBrain(Protocol):
    def plan(
        self, message: str, *, schema: str, feedback: str | None = None
    ) -> ReadPlan:
        ...


def schema_text() -> str:
    """A compact, model-readable description of the live schema."""

    from database.read_planner import schema_text as live_schema_text

    return live_schema_text()


def _system_prompt(schema: str) -> str:
    return f"""
You are the read-query planner of a task database. You do not write SQL and
you do not execute anything. You return one JSON plan and the runtime compiles
and validates it.

Schema:
{schema}

Return ONLY valid JSON with this shape:
{{
  "columns": ["id", "task_title", "assigned_to", "status", "progress", ...],
  "filters": [
    {{"column": "...", "operator": "eq|ne|in|contains|starts_with|gt|gte|lt|lte|is_null|not_null", "value": ...}}
  ],
  "order_by": [{{"column": "id", "direction": "asc|desc"}}],
  "limit": 100,
  "requested_field": "semantic field id or null",
  "explanation": "short reason"
}}

Rules:
1. Use ONLY columns from the schema. Never invent a column.
2. "pending", "outstanding", "open", "unfinished", "remaining", "not done"
   normally mean the open work: status in ["PENDING", "IN_PROGRESS"].
   Use the exact PENDING value only when the user says "status is pending",
   "only pending", or otherwise refers to the stored status value.
3. "completed", "done", "finished" mean status = COMPLETED.
   "in progress", "ongoing", "active" mean status = IN_PROGRESS.
4. For a list request, use filters. For a question about one known task,
   filter by id (or title) and select only the needed columns.
5. With no usable filter, return all tasks (empty filters).
6. Use "contains" for a title or assignee phrase; never inline SQL.
7. Never invent a task id or a value that the user did not state.
8. If a previous attempt returned no rows, adjust the filters using the
   observation. Do not repeat the same plan. If the data genuinely has no
   matching row, return the closest useful query instead.
9. Return exactly one JSON object, with no text or Markdown.
""".strip()


class CohereReadBrain:
    """Production brain: structured JSON output through Cohere."""

    def plan(
        self, message: str, *, schema: str, feedback: str | None = None
    ) -> ReadPlan:
        from ai.text_extractor import ask_model

        user_input = message

        if feedback:
            user_input = (
                f"{message}\n\n"
                f"The previous attempt failed or returned nothing:\n{feedback}\n"
                "Return a corrected plan."
            )

        raw = ask_model(_system_prompt(schema), user_input, ReadPlanModel)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ReadPlanError("The model returned invalid JSON.") from error

        return plan_from_dict(payload)


def _status_counts(employee: str | None = None) -> dict:
    """Count the stored tasks by status, scoped to the employee."""

    try:
        rows = (
            task_repository.search_tasks(owner=employee)
            if employee
            else task_repository.get_all_tasks()
        )
    except Exception:
        return {}

    counts: dict[str, int] = {}

    for row in rows:
        status = getattr(row, "status", None) or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1

    return counts


def _scope_rows(rows: list, employee: str | None) -> list:
    if not employee:
        return rows

    owner = employee.strip().lower()

    return [
        row
        for row in rows
        if str(row.get("assigned_to") or "").strip().lower() == owner
    ]


def _default_brain() -> ReadBrain | None:
    flag = os.getenv("AGENTIC_READ", "").strip().lower()

    if flag not in {"1", "true", "yes", "on"}:
        return None

    return CohereReadBrain()


def run_agentic_read(
    message: str,
    *,
    employee: str | None = None,
    brain: ReadBrain | None = None,
    executor=None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict | None:
    """Plan, validate, execute and repair a read request.

    Returns ``None`` when the agent is disabled and no brain is supplied, so
    a caller can keep its existing, deterministic path.
    """

    if brain is None:
        brain = _default_brain()

    if brain is None:
        return None

    if executor is None:
        executor = task_repository.run_sql_operation

    schema = schema_text()
    feedback = None
    attempts = []
    last_plan = None

    for step in range(max_steps):
        try:
            plan = brain.plan(message, schema=schema, feedback=feedback)
            operation = compile_plan(plan, owner=employee, intent="LIST_TASKS")
            validate_operation(operation)
        except Exception as error:
            feedback = (
                f"Plan rejected: {error}. Use only schema columns and the "
                "listed operators."
            )
            attempts.append({"step": step + 1, "error": str(error)})
            continue

        try:
            result = executor(operation)
        except Exception as error:
            logger.warning("Agentic read failed to execute: %s", error)
            feedback = f"The execution failed: {error}."
            attempts.append({"step": step + 1, "error": str(error)})
            continue

        if result.get("status") != "success":
            feedback = (
                "The database rejected the query: "
                f"{result.get('message') or 'unknown error'}."
            )
            attempts.append({"step": step + 1, "error": feedback})
            continue

        rows = _scope_rows(result.get("rows") or [], employee)

        if rows:
            return {
                "status": "success",
                "rows": rows,
                "plan": plan.as_dict(),
                "attempts": attempts + [{"step": step + 1, "rows": len(rows)}],
            }

        counts = _status_counts(employee)
        feedback = (
            "The plan was valid but returned 0 rows. "
            f"Current tasks by status: {counts or 'no tasks stored'}. "
            "Adjust the filters if the user's wording implies open work, or "
            "return the plan that best represents the request."
        )
        attempts.append({"step": step + 1, "rows": 0, "counts": counts})
        last_plan = plan

    return {
        "status": "no_results",
        "rows": [],
        "plan": last_plan.as_dict() if last_plan is not None else None,
        "attempts": attempts,
    }
