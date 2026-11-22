"""Conversation-memory MCP tools scoped to the authenticated employee."""

from __future__ import annotations

from auth.permissions import TASKS_READ
from mcp_server.registry.tool_registry import MCPToolSpec
from mcp_server.tools.common import error, object_schema
from services.conversation_memory import get_recent_messages
from services.task_scope import use_task_actor


PERMISSIONS = frozenset({TASKS_READ})


def _context(arguments, context):
    from services.database_agent import ToolContext

    conversation_id = str(arguments.get("conversation_id") or "").strip()

    if not conversation_id:
        raise ValueError("conversation_id is required.")

    return ToolContext(
        message="",
        employee=context.employee.name,
        user_id=context.user_id,
        conversation_id=conversation_id,
    ), conversation_id


def retrieve_conversation_context(arguments, context):
    from services.conversation_memory import get_conversation_memory
    from services.database_agent import _context_snapshot

    try:
        tool_context, conversation_id = _context(arguments, context)
        memory = get_conversation_memory(context.user_id, conversation_id)

        if memory is None:
            return {
                "status": "not_found",
                "message": "No stored conversation context was found.",
            }

        memory.setdefault("context", {})["history"] = get_recent_messages(
            context.user_id, conversation_id
        )
        tool_context.memory = memory

        with use_task_actor(context.actor):
            return _context_snapshot(tool_context)
    except ValueError as failure:
        return error(str(failure))


def update_conversation_context(arguments, context):
    from services.database_agent import _update_context

    try:
        tool_context, _ = _context(arguments, context)
    except ValueError as failure:
        return error(str(failure))

    with use_task_actor(context.actor):
        return _update_context(arguments, tool_context)


def build_agent_tools() -> list[MCPToolSpec]:
    conversation_id = {
        "type": "string",
        "description": "The conversation this memory belongs to.",
    }

    return [
        MCPToolSpec(
            name="retrieve_conversation_context",
            title="Retrieve conversation context",
            description=(
                "Return employee-scoped active task, candidates, history and "
                "previous agent trace for one conversation."
            ),
            input_schema=object_schema(
                {"conversation_id": conversation_id},
                ("conversation_id",),
            ),
            permissions=PERMISSIONS,
            handler=retrieve_conversation_context,
        ),
        MCPToolSpec(
            name="update_conversation_context",
            title="Update conversation context",
            description=(
                "Store an active task reference or short note for one "
                "conversation. Task ownership is revalidated."
            ),
            input_schema=object_schema(
                {
                    "conversation_id": conversation_id,
                    "active_task_id": {"type": "integer"},
                    "note": {"type": "string", "maxLength": 300},
                },
                ("conversation_id",),
            ),
            permissions=PERMISSIONS,
            handler=update_conversation_context,
        ),
    ]
