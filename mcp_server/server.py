"""Streamable HTTP MCP server for the FnW Task Manager."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

from mcp_server.configuration import load_mcp_settings, normalize_public_mcp_resource_url
from mcp_server.prompts.agent_prompts import get_prompt, list_prompts
from mcp_server.registry.tool_registry import MCPToolSpec, tool_registry
from mcp_server.resources.skill_resources import ALL_RESOURCE_MAP, list_resources
from mcp_server.security.authentication import MCPAuthenticationMiddleware
from mcp_server.security.execution_context import (
    MCPAuthenticationError,
    execution_context_for_request,
)
from mcp_server.tools.agent_tools import build_agent_tools
from mcp_server.tools.database_tools import build_database_tools
from mcp_server.tools.employee_tools import build_employee_tools
from mcp_server.tools.notification_tools import build_notification_tools
from mcp_server.tools.task_tools import build_task_tools
from services.task_scope import use_task_actor

logger = logging.getLogger(__name__)

settings = load_mcp_settings()

tool_registry.register_many(
    build_task_tools()
    + build_employee_tools()
    + build_notification_tools()
    + build_database_tools()
    + build_agent_tools()
)

mcp_server = Server(
    settings.server_name,
    version="1.0.0",
    instructions=(
        "The FnW Task Manager MCP server exposes task, employee, notification, "
        "read-only database and conversation tools. Identity always comes from "
        "the bearer token. Employees can manage only their own tasks and can "
        "never reassign them."
    ),
)


def _request_context():
    try:
        return mcp_server.request_context
    except LookupError:
        return None


def _execution_context():
    request_context = _request_context()
    request = request_context.request if request_context is not None else None
    return execution_context_for_request(request)


def _tool_annotations(spec: MCPToolSpec) -> ToolAnnotations | None:
    read_only = spec.name.startswith(("get_", "list_", "search_", "inspect_", "validate_", "retrieve_"))
    destructive = spec.name == "delete_task"

    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only,
    )


def _mcp_tool(spec: MCPToolSpec) -> Tool:
    return Tool(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        inputSchema=spec.input_schema,
        outputSchema=spec.output_schema,
        annotations=_tool_annotations(spec),
        _meta={
            "fnw/permissions": sorted(spec.permissions),
            "securitySchemes": [{"type": "oauth2", "scopes": ["mcp:tools"]}],
        },
    )


def _tool_error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"status": "error", "message": message}))],
        isError=True,
    )


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    context = _execution_context()
    return [_mcp_tool(spec) for spec in tool_registry.list(context=context)]


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    try:
        context = _execution_context()
    except MCPAuthenticationError as error:
        return _tool_error(str(error))

    try:
        spec = tool_registry.get(name, context=context)
    except KeyError:
        return _tool_error("Unknown tool or insufficient permission.")

    try:
        with use_task_actor(context.actor):
            result = spec.handler(arguments or {}, context)
    except Exception as error:
        logger.exception("MCP tool %s failed.", name)
        return _tool_error(f"The tool failed: {error}")

    if not isinstance(result, dict):
        return _tool_error("The tool returned an invalid result.")

    return result


@mcp_server.list_resources()
async def list_resources_handler():
    _execution_context()
    return list_resources()


@mcp_server.read_resource()
async def read_resource(uri) -> list[ReadResourceContents]:
    _execution_context()
    resource = ALL_RESOURCE_MAP.get(str(uri))

    if resource is None:
        raise ValueError(f"Unknown resource: {uri}")

    return [resource.read()]


@mcp_server.list_prompts()
async def list_prompts_handler():
    _execution_context()
    return list_prompts()


@mcp_server.get_prompt()
async def get_prompt_handler(name: str, arguments):
    _execution_context()
    return get_prompt(name, arguments)


_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=settings.dns_rebinding_protection,
    allowed_hosts=list(settings.allowed_hosts),
    allowed_origins=list(settings.allowed_origins),
)

session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    json_response=settings.json_response,
    stateless=settings.stateless,
    security_settings=_security,
    max_request_body_size=settings.max_body_size,
    session_idle_timeout=settings.session_idle_timeout or None,
)

class _SessionManagerASGI:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


mcp_asgi_app = MCPAuthenticationMiddleware(
    _SessionManagerASGI(session_manager),
    settings,
)


@asynccontextmanager
async def mcp_lifespan(app=None):
    async with session_manager.run():
        yield


def _normalize_mcp_path(path: str) -> str:
    value = "/" + (path or "/mcp").strip("/")
    return value or "/mcp"


def _metadata_paths(mcp_path: str) -> tuple[str, str]:
    """Return (path-specific, root) protected-resource metadata paths."""

    normalized = _normalize_mcp_path(mcp_path)
    suffix = normalized.lstrip("/")  # e.g. "mcp"
    path_specific = f"/.well-known/oauth-protected-resource/{suffix}"
    root = "/.well-known/oauth-protected-resource"
    return path_specific, root


def _public_resource_url(request) -> str | None:
    """Absolute, externally advertised MCP resource URI.

    This is used for OAuth protected-resource metadata (RFC 9728). It must
    identify the public MCP resource, not the internal Docker address.
    """

    configured = normalize_public_mcp_resource_url(
        settings.public_resource_url,
        mcp_path=settings.path,
    )
    if configured:
        return configured

    # Fallback: infer from request headers. Safe only when behind a trusted
    # proxy that sets X-Forwarded-* correctly; operators should set
    # MCP_PUBLIC_RESOURCE_URL explicitly for public deployments.
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or (request.headers.get("host") or "").strip()
    if not host:
        return None
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    mcp_path = _normalize_mcp_path(settings.path)
    return f"{proto}://{host}{mcp_path}"


def _protected_resource_metadata(request) -> dict | None:
    resource = _public_resource_url(request)
    if not resource:
        return None

    # MCP supports dedicated static credentials and OAuth-issued MCP access tokens.
    # Authorization server metadata is hosted by this same FastAPI app.
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(resource)
    issuer = urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
        "resource_name": settings.server_name,
    }


async def protected_resource_metadata_handler(request):
    from starlette.responses import JSONResponse

    body = _protected_resource_metadata(request)
    if body is None:
        return JSONResponse(
            {
                "error": "server_misconfigured",
                "detail": (
                    "MCP_PUBLIC_RESOURCE_URL must be configured with the "
                    "externally accessible MCP resource URI."
                ),
            },
            status_code=503,
        )
    return JSONResponse(body, media_type="application/json")


def mount_mcp(app) -> bool:
    if not settings.enabled:
        return False

    if settings.transport != "streamable-http":  # pragma: no cover
        raise RuntimeError(
            "Only the streamable-http MCP transport is supported."
        )

    path_specific, root = _metadata_paths(settings.path)
    # Register both RFC 9728 discovery endpoints. They are public metadata;
    # MCP auth remains enforced on /mcp itself.
    for path in {path_specific, root}:
        try:
            app.add_route(
                path,
                protected_resource_metadata_handler,
                methods=["GET", "OPTIONS"],
                include_in_schema=False,
            )
        except Exception:
            logger.exception("Failed to register MCP metadata route: %s", path)

    app.mount(settings.path, mcp_asgi_app)
    return True
