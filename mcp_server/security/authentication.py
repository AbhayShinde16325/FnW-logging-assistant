"""ASGI authentication guard for the mounted MCP endpoint."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.configuration import MCPSettings
from mcp_server.security.execution_context import (
    MCPAuthenticationError,
    execution_context_for_request,
)


class MCPAuthenticationMiddleware:
    def __init__(self, app, settings: MCPSettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self.settings.require_auth:
            request = Request(scope, receive)

            try:
                context = execution_context_for_request(request)
            except MCPAuthenticationError as error:
                response = JSONResponse(
                    {"error": "unauthorized", "detail": str(error)},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return

            scope.setdefault("state", {})["mcp_context"] = context
            request.state.mcp_context = context

        await self.app(scope, receive, send)


def unauthorized_response(scope, receive, send, error: MCPAuthenticationError):
    response = JSONResponse(
        {"error": "unauthorized", "detail": str(error)},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
    return response(scope, receive, send)
