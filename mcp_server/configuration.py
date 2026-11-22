"""MCP server and client configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MCPSettings:
    enabled: bool = False
    transport: str = "streamable-http"
    server_name: str = "fnw-task-manager"
    path: str = "/mcp"
    require_auth: bool = True
    server_url: str = "http://127.0.0.1:8000/mcp"
    stateless: bool = True
    json_response: bool = True
    tool_cache_seconds: float = 300.0
    max_body_size: int = 4 * 1024 * 1024
    session_idle_timeout: float = 900.0
    dns_rebinding_protection: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    # Public, externally advertised MCP resource URI (e.g. tunnel URL).
    # Used only for OAuth protected-resource metadata; never for internal routing.
    public_resource_url: str = ""


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )


@lru_cache(maxsize=1)
def load_mcp_settings() -> MCPSettings:
    return MCPSettings(
        enabled=_bool("MCP_ENABLED", False),
        transport=os.getenv("MCP_TRANSPORT", "streamable-http").strip().lower(),
        server_name=os.getenv("MCP_SERVER_NAME", "fnw-task-manager").strip(),
        path=os.getenv("MCP_PATH", "/mcp").strip() or "/mcp",
        require_auth=_bool("MCP_REQUIRE_AUTH", True),
        server_url=os.getenv(
            "MCP_SERVER_URL", "http://127.0.0.1:8000/mcp"
        ).strip(),
        stateless=_bool("MCP_STATELESS", True),
        json_response=_bool("MCP_JSON_RESPONSE", True),
        tool_cache_seconds=max(0.0, _float("MCP_TOOL_CACHE_SECONDS", 300.0)),
        max_body_size=max(1024, _int("MCP_MAX_BODY_SIZE", 4 * 1024 * 1024)),
        session_idle_timeout=max(
            0.0, _float("MCP_SESSION_IDLE_TIMEOUT", 900.0)
        ),
        dns_rebinding_protection=_bool(
            "MCP_DNS_REBINDING_PROTECTION", False
        ),
        allowed_hosts=_csv("MCP_ALLOWED_HOSTS"),
        allowed_origins=_csv("MCP_ALLOWED_ORIGINS"),
        public_resource_url=os.getenv("MCP_PUBLIC_RESOURCE_URL", "").strip(),
    )


def clear_mcp_settings_cache() -> None:
    load_mcp_settings.cache_clear()
