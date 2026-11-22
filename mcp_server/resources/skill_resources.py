"""Existing repository skills exposed through MCP resources."""

from __future__ import annotations

from mcp.types import Resource

from mcp_server.resources.application_resources import (
    APPLICATION_RESOURCES,
    RESOURCE_MAP,
    PROJECT_ROOT,
    ResourceSpec,
)


def discover_skill_resources() -> tuple[ResourceSpec, ...]:
    skills_root = PROJECT_ROOT / ".agents" / "skills"
    discovered = []

    if not skills_root.exists():
        return tuple(discovered)

    for skill in sorted(skills_root.glob("*/SKILL.md")):
        uri = f"fnw://skills/{skill.parent.name}"
        discovered.append(
            ResourceSpec(
                uri=uri,
                name=f"Skill: {skill.parent.name}",
                description=f"Repository skill instructions for {skill.parent.name}.",
                path=skill,
            )
        )

    return tuple(discovered)


SKILL_RESOURCES = discover_skill_resources()
ALL_RESOURCES = tuple(APPLICATION_RESOURCES) + SKILL_RESOURCES
ALL_RESOURCE_MAP = {**RESOURCE_MAP, **{r.uri: r for r in SKILL_RESOURCES}}


def list_resources() -> list[Resource]:
    return [resource.as_mcp_resource() for resource in ALL_RESOURCES]
