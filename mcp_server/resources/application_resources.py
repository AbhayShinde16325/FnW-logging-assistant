"""Application documentation exposed as MCP resources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import Resource

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    description: str
    path: Path
    mime_type: str = "text/markdown"

    def as_mcp_resource(self) -> Resource:
        return Resource(
            uri=self.uri,
            name=self.name,
            description=self.description,
            mimeType=self.mime_type,
        )

    def read(self) -> ReadResourceContents:
        text = self.path.read_text(encoding="utf-8")
        return ReadResourceContents(content=text, mime_type=self.mime_type)


def _resource(uri: str, name: str, description: str, relative_path: str):
    return ResourceSpec(
        uri=uri,
        name=name,
        description=description,
        path=PROJECT_ROOT / relative_path,
    )


APPLICATION_RESOURCES = (
    _resource(
        "fnw://docs/readme",
        "Application README",
        "Setup, authentication, API and deployment documentation.",
        "README.md",
    ),
    _resource(
        "fnw://docs/database-schema",
        "Database schema",
        "The authoritative tasks schema and value rules.",
        "DATABASE_SCHEMA.md",
    ),
    _resource(
        "fnw://docs/architecture",
        "MCP architecture",
        "MCP server, provider adapter and authorization architecture.",
        "docs/MCP_ARCHITECTURE.md",
    ),
    _resource(
        "fnw://docs/task-management",
        "Task management instructions",
        "How the assistant should create, read, update and delete tasks.",
        ".agents/skills/sql-generation/SKILL.md",
    ),
    _resource(
        "fnw://docs/reminders",
        "Reminder system documentation",
        "Reminder validation, scheduling and notification rules.",
        ".agents/skills/reminder-notifications/SKILL.md",
    ),
)

RESOURCE_MAP = {resource.uri: resource for resource in APPLICATION_RESOURCES}
