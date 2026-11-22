"""Read-only database inspection tools for MCP.

Writes are deliberately not exposed as raw SQL. Task writes go through the
task-management tools so validation, ownership and confirmation remain in the
existing service layer.
"""

from __future__ import annotations

from auth.permissions import TASKS_READ
from mcp_server.registry.tool_registry import MCPToolSpec
from mcp_server.tools.common import object_schema
from services.task_scope import use_task_actor


PERMISSIONS = frozenset({TASKS_READ})


def _tool_context(arguments, context):
    from services.database_agent import ToolContext

    return ToolContext(
        message=str(arguments.get("message") or ""),
        employee=context.employee.name,
        user_id=context.user_id,
        conversation_id=str(arguments.get("conversation_id") or "") or None,
    )


def inspect_database_schema(arguments, context):
    from services.database_agent import _schema_tool

    with use_task_actor(context.actor):
        return _schema_tool()


def inspect_table_definition(arguments, context):
    from services.database_agent import _table_definition

    if str(arguments.get("table") or "").strip().lower() != "tasks":
        return {
            "status": "error",
            "message": "Only the tasks table may be inspected.",
        }

    with use_task_actor(context.actor):
        return _table_definition()


def validate_sql_query(arguments, context):
    from services.database_agent import _validate_tool

    with use_task_actor(context.actor):
        return _validate_tool(arguments)


def execute_read_query(arguments, context):
    from services.database_agent import _read_query

    with use_task_actor(context.actor):
        return _read_query(arguments, _tool_context(arguments, context))


def build_database_tools() -> list[MCPToolSpec]:
    return [
        MCPToolSpec(
            name="inspect_database_schema",
            title="Inspect database schema",
            description=(
                "Return the authoritative schema and value rules for the only "
                "permitted table, tasks."
            ),
            input_schema=object_schema({}),
            permissions=PERMISSIONS,
            handler=inspect_database_schema,
        ),
        MCPToolSpec(
            name="inspect_table_definition",
            title="Inspect table definition",
            description="Return tasks table columns, types and defaults.",
            input_schema=object_schema(
                {"table": {"type": "string", "enum": ["tasks"]}},
                ("table",),
            ),
            permissions=PERMISSIONS,
            handler=inspect_table_definition,
        ),
        MCPToolSpec(
            name="validate_sql_query",
            title="Validate SQL",
            description=(
                "Validate one SQL statement without executing it. Only the "
                "tasks table is permitted."
            ),
            input_schema=object_schema(
                {
                    "sql": {"type": "string"},
                    "params": {"type": "object"},
                },
                ("sql",),
            ),
            permissions=PERMISSIONS,
            handler=validate_sql_query,
        ),
        MCPToolSpec(
            name="execute_read_query",
            title="Execute a read query",
            description=(
                "Execute one validated, read-only SELECT against tasks. "
                "Employee-scoped calls must bind their own employee id."
            ),
            input_schema=object_schema(
                {
                    "sql": {"type": "string"},
                    "params": {"type": "object"},
                    "max_rows": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                ("sql",),
            ),
            permissions=PERMISSIONS,
            handler=execute_read_query,
        ),
    ]
