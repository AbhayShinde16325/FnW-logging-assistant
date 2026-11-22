"""Permission checks used by MCP tools and discovery."""

from __future__ import annotations

from collections.abc import Iterable

from mcp_server.security.execution_context import MCPExecutionContext


class MCPAuthorizationError(Exception):
    """The authenticated employee may not invoke this capability."""


def has_permissions(
    context: MCPExecutionContext,
    permissions: Iterable[str],
) -> bool:
    required = set(permissions)
    return not required or required.issubset(context.employee.permissions)


def require_permissions(
    context: MCPExecutionContext,
    permissions: Iterable[str],
) -> None:
    if not has_permissions(context, permissions):
        raise MCPAuthorizationError(
            "You do not have permission to perform this action."
        )
