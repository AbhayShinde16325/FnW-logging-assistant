"""Convert MCP tool definitions into provider function schemas."""

from __future__ import annotations

from collections.abc import Iterable


def required_permissions(tool) -> set[str]:
    meta = getattr(tool, "meta", None) or {}
    values = meta.get("fnw/permissions") or []
    return {str(value) for value in values}


def to_function_tool(tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or tool.name,
            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


def to_function_tools(
    tools: Iterable,
    permissions: Iterable[str],
) -> list[dict]:
    allowed = set(permissions)
    converted = []

    for tool in tools:
        required = required_permissions(tool)

        if required and not required.issubset(allowed):
            continue

        converted.append(to_function_tool(tool))

    return converted
