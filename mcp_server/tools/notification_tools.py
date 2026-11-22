"""Notification MCP tools backed by the existing notification service."""

from __future__ import annotations

from auth.permissions import TASKS_READ
from mcp_server.registry.tool_registry import MCPToolSpec
from mcp_server.tools.common import error, object_schema, ok
from services import notification_service


PERMISSIONS = frozenset({TASKS_READ})


def list_notifications(arguments, context):
    limit = min(200, max(1, int(arguments.get("limit") or 50)))
    offset = max(0, int(arguments.get("offset") or 0))
    unread_only = bool(arguments.get("unread_only") or False)

    rows, total = notification_service.list_notifications(
        context.employee.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )

    return ok(
        count=len(rows),
        total=total,
        unread_count=notification_service.unread_notification_count(
            context.employee.id
        ),
        notifications=[
            notification_service.serialize_notification(row) for row in rows
        ],
    )


def get_unread_notification_count(arguments, context):
    return ok(
        unread_count=notification_service.unread_notification_count(
            context.employee.id
        )
    )


def mark_notification_read(arguments, context):
    notification_id = int(arguments["notification_id"])

    if not notification_service.mark_notification_read(
        context.employee.id, notification_id
    ):
        return error("The notification was not found.", status="not_found")

    return ok(
        notification_id=notification_id,
        unread_count=notification_service.unread_notification_count(
            context.employee.id
        ),
    )


def mark_all_notifications_read(arguments, context):
    updated = notification_service.mark_all_notifications_read(
        context.employee.id
    )
    return ok(updated=updated, unread_count=0)


def build_notification_tools() -> list[MCPToolSpec]:
    return [
        MCPToolSpec(
            name="list_notifications",
            title="List notifications",
            description="List the authenticated employee's notifications only.",
            input_schema=object_schema(
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                    "unread_only": {"type": "boolean"},
                }
            ),
            permissions=PERMISSIONS,
            handler=list_notifications,
        ),
        MCPToolSpec(
            name="get_unread_notification_count",
            title="Get unread notification count",
            description="Return the authenticated employee's unread count.",
            input_schema=object_schema({}),
            permissions=PERMISSIONS,
            handler=get_unread_notification_count,
        ),
        MCPToolSpec(
            name="mark_notification_read",
            title="Mark a notification read",
            description="Mark one notification of the authenticated employee read.",
            input_schema=object_schema(
                {"notification_id": {"type": "integer"}},
                ("notification_id",),
            ),
            permissions=PERMISSIONS,
            handler=mark_notification_read,
        ),
        MCPToolSpec(
            name="mark_all_notifications_read",
            title="Mark all notifications read",
            description="Mark all notifications of the authenticated employee read.",
            input_schema=object_schema({}),
            permissions=PERMISSIONS,
            handler=mark_all_notifications_read,
        ),
    ]
