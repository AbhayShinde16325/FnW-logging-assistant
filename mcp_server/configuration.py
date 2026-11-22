"""MCP server and client configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit


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
    app_env: str = "development"
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
    # Public, externally advertised MCP resource URI (e.g. tunnel URL or
    # direct production HTTPS origin). Used only for OAuth protected-resource
    # metadata; never for internal routing or local MCP adapter calls.
    public_resource_url: str = ""
    tunnel_enabled: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )


def _normalize_app_env(raw: str | None) -> str:
    value = (raw or "development").strip().lower()
    if value in {"prod", "production"}:
        return "production"
    if value in {"dev", "development", "local", "test"}:
        return "development"
    return value or "development"


def _normalize_mcp_path(path: str | None) -> str:
    value = "/" + (path or "/mcp").strip("/")
    return value or "/mcp"


def normalize_public_mcp_resource_url(raw: str | None, *, mcp_path: str) -> str:
    """Return a canonical public MCP resource URI for metadata.

    RFC 9728 protected-resource metadata advertises a resource URI. For the
    direct Render/public endpoint we accept either the public origin or the
    full /mcp resource URL and canonicalize to the full resource URL.
    """

    value = (raw or "").strip()
    if not value:
        return ""

    normalized_path = _normalize_mcp_path(mcp_path)
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value.rstrip("/")

    path = parts.path or ""
    if path in {"", "/"}:
        path = normalized_path
    elif path.rstrip("/") == normalized_path:
        path = normalized_path
    else:
        path = path.rstrip("/")

    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


@lru_cache(maxsize=1)
def load_mcp_settings() -> MCPSettings:
    app_env = _normalize_app_env(os.getenv("APP_ENV"))
    path = _normalize_mcp_path(os.getenv("MCP_PATH", "/mcp"))
    tunnel_enabled = _bool("MCP_TUNNEL_ENABLED", app_env != "production")
    return MCPSettings(
        app_env=app_env,
        enabled=_bool("MCP_ENABLED", False),
        transport=os.getenv("MCP_TRANSPORT", "streamable-http").strip().lower(),
        server_name=os.getenv("MCP_SERVER_NAME", "fnw-task-manager").strip(),
        path=path,
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
        public_resource_url=normalize_public_mcp_resource_url(
            os.getenv("MCP_PUBLIC_RESOURCE_URL", ""),
            mcp_path=path,
        ),
        tunnel_enabled=tunnel_enabled,
    )


def clear_mcp_settings_cache() -> None:
    load_mcp_settings.cache_clear()
