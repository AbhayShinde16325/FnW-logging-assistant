"""Provider-neutral prompts for common task-manager workflows."""

from __future__ import annotations

from mcp.types import GetPromptResult, Prompt, PromptArgument, PromptMessage, TextContent


_PROMPTS = {
    "task_creation": {
        "description": "Instructions for creating a task safely.",
        "arguments": ("employee_name",),
        "text": (
            "You are the FnW task creation assistant. Use create_task. "
            "Never invent a task id or employee id. Employees create tasks "
            "only for themselves; managers and administrators may assign an "
            "active employee. Ask for a title when it is missing. Use ISO "
            "dates (YYYY-MM-DD) and times (HH:MM). Do not claim a task was "
            "created until the tool returns status=success."
        ),
    },
    "task_update": {
        "description": "Instructions for updating a task safely.",
        "arguments": ("employee_name",),
        "text": (
            "You are the FnW task update assistant. Read the task before "
            "changing it. Use the narrow update_task_status, "
            "update_task_progress, update_task_deadline or update_task_reminder "
            "tool when possible. The confirmation argument must come from the "
            "employee's exact message; never invent CONFIRM UPDATE. Employees "
            "may edit only their own tasks and may never reassign a task."
        ),
    },
    "database_analysis": {
        "description": "Instructions for read-only database analysis.",
        "arguments": (),
        "text": (
            "You are analysing the FnW tasks database. Only the tasks table is "
            "permitted. Use inspect_database_schema, validate_sql_query and "
            "execute_read_query. Reads are always scoped to the authenticated "
            "employee unless they have tasks.read_all. Never invent a column, "
            "task id or value. Never request a raw write query."
        ),
    },
    "reminder_management": {
        "description": "Instructions for reminder management.",
        "arguments": ("employee_name",),
        "text": (
            "You are the FnW reminder assistant. Reminder date/time and "
            "deadline date/time are different fields. Use the task reminder "
            "tools; do not substitute updated_at or a deadline for a reminder. "
            "The existing reminder service validates dates and schedules "
            "notifications. Updates require the employee's exact CONFIRM UPDATE "
            "phrase and task ownership."
        ),
    },
}


def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=name,
            description=value["description"],
            arguments=[
                PromptArgument(name=argument, required=False)
                for argument in value["arguments"]
            ],
        )
        for name, value in sorted(_PROMPTS.items())
    ]


def get_prompt(name: str, arguments: dict[str, str] | None = None) -> GetPromptResult:
    value = _PROMPTS.get(name)

    if value is None:
        raise ValueError(f"Unknown prompt: {name}")

    supplied = arguments or {}
    employee = supplied.get("employee_name", "").strip()
    text = value["text"]

    if employee:
        text = f"Authenticated employee: {employee}.\n\n{text}"

    return GetPromptResult(
        description=value["description"],
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=text),
            )
        ],
    )
