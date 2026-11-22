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

    def _resource_metadata_url(self, request: Request) -> str | None:
        try:
            from mcp_server.server import _metadata_paths, _public_resource_url

            resource = _public_resource_url(request)
            if not resource:
                return None
            path_specific, _root = _metadata_paths(self.settings.path)
            # RFC 9728 protected resource metadata endpoint
            from urllib.parse import urlsplit, urlunsplit

            p = urlsplit(resource)
            return urlunsplit((p.scheme, p.netloc, path_specific, "", ""))
        except Exception:
            return None

    def _www_authenticate(self, request: Request, error: MCPAuthenticationError) -> str:
        meta = self._resource_metadata_url(request)
        parts = ['Bearer realm="fnw-mcp"']
        detail = str(error).lower()
        if "required" in detail or "missing" in detail:
            parts.append('error="invalid_token"')
        else:
            parts.append('error="invalid_token"')
        parts.append(f'error_description="{str(error)}"')
        if meta:
            parts.append(f'resource_metadata="{meta}"')
        return ", ".join(parts)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and self.settings.require_auth:
            request = Request(scope, receive)

            try:
                context = execution_context_for_request(request)
            except MCPAuthenticationError as error:
                response = JSONResponse(
                    {"error": "unauthorized", "detail": str(error)},
                    status_code=401,
                    headers={"WWW-Authenticate": self._www_authenticate(request, error)},
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
