"""Central registry for dynamic MCP tool discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp_server.security.authorization import has_permissions
from mcp_server.security.execution_context import MCPExecutionContext

_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    permissions: frozenset[str]
    handler: Callable[[dict[str, Any], MCPExecutionContext], dict[str, Any]]
    title: str | None = None
    output_schema: dict[str, Any] | None = None

    def __post_init__(self):
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"Invalid MCP tool name: {self.name}")

        if not isinstance(self.input_schema, dict):
            raise ValueError(f"Tool {self.name} needs an object schema.")

        if self.input_schema.get("type") != "object":
            raise ValueError(f"Tool {self.name} schema must be an object.")

    def visible_to(self, context: MCPExecutionContext) -> bool:
        return has_permissions(context, self.permissions)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, MCPToolSpec] = {}

    def register(self, spec: MCPToolSpec) -> MCPToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate MCP tool name: {spec.name}")

        self._tools[spec.name] = spec
        return spec

    def register_many(self, specs) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str, *, context: MCPExecutionContext) -> MCPToolSpec:
        spec = self._tools.get(name)

        if spec is None or not spec.visible_to(context):
            raise KeyError(name)

        return spec

    def list(self, *, context: MCPExecutionContext | None = None) -> list[MCPToolSpec]:
        specs = list(self._tools.values())

        if context is not None:
            specs = [spec for spec in specs if spec.visible_to(context)]

        return sorted(specs, key=lambda spec: spec.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


tool_registry = ToolRegistry()
