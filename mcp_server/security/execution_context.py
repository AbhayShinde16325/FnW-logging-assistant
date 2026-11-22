"""Trusted, per-request MCP execution identity.

The bearer token is the only source of identity. Tool arguments, resource
arguments, conversation memory and model output never establish a principal.

The /mcp endpoint authenticates exclusively with dedicated ``fnw_mcp_...``
credentials. Employee access JWTs, refresh tokens and browser sessions are
never accepted here; REST/JWT authentication remains unchanged elsewhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from starlette.requests import Request

from auth.service import AuthenticatedEmployee
from services.task_scope import TaskActor

logger = logging.getLogger(__name__)


class MCPAuthenticationError(Exception):
    """The request has no valid authenticated employee."""


@dataclass(frozen=True)
class MCPExecutionContext:
    employee: AuthenticatedEmployee
    actor: TaskActor
    access_token: str

    @property
    def user_id(self) -> str:
        return f"employee:{self.employee.id}"


def _authorization_diagnostics(headers) -> dict:
    """Safe header diagnostics. Never includes token, hash, or secret values."""

    raw = headers.get("authorization") if headers is not None else None
    present = bool(raw)

    if not present:
        return {
            "authorization_present": False,
            "bearer_scheme": False,
            "mcp_prefix": False,
        }

    value = str(raw)
    scheme_part, _, token_part = value.partition(" ")
    token = token_part.strip() if token_part else ""
    bearer_scheme = scheme_part.strip().lower() == "bearer" and bool(token)

    return {
        "authorization_present": True,
        "bearer_scheme": bearer_scheme,
        "mcp_prefix": bearer_scheme and token.startswith("fnw_mcp_"),
    }


def bearer_token_from_headers(headers) -> str | None:
    diagnostics = _authorization_diagnostics(headers)

    if not diagnostics["bearer_scheme"]:
        return None

    _, _, token = str(headers.get("authorization") or "").partition(" ")
    return token.strip() or None


def execution_context_from_token(token: str | None) -> MCPExecutionContext:
    """Authenticate an MCP request using a dedicated MCP credential only."""

    if not token:
        raise MCPAuthenticationError("Authentication required.")

    from auth.mcp_credentials import (
        MCP_CREDENTIAL_PREFIX,
        MCPCredentialValidationError,
        resolve_employee_for_mcp_credential,
    )
    from auth.mcp_oauth import MCPOAuthError, validate_mcp_oauth_access_token

    if token.startswith(MCP_CREDENTIAL_PREFIX):
        try:
            employee, _credential_id = resolve_employee_for_mcp_credential(token)
        except MCPCredentialValidationError as error:
            raise MCPAuthenticationError(str(error)) from error
        return MCPExecutionContext(
            employee=employee,
            actor=TaskActor.from_employee(employee),
            access_token=token,
        )

    # ChatGPT/remote MCP OAuth access token (JWT, MCP-specific type).
    try:
        from mcp_server.configuration import load_mcp_settings
        expected_resource = (load_mcp_settings().public_resource_url or "").strip() or None
        employee, _session_id = validate_mcp_oauth_access_token(token, expected_resource=expected_resource)
    except MCPOAuthError as error:
        logger.warning("MCP OAuth token rejected: %s", error.error)
        raise MCPAuthenticationError("A valid MCP credential or OAuth token is required.") from error
    except Exception:
        raise MCPAuthenticationError("A valid MCP credential or OAuth token is required.")

    return MCPExecutionContext(
        employee=employee,
        actor=TaskActor.from_employee(employee),
        access_token=token,
    )


def _rejection_reason(headers, error: MCPAuthenticationError) -> str:
    diagnostics = _authorization_diagnostics(headers)
    detail = str(error)

    if not diagnostics["authorization_present"]:
        return "missing_authorization_header"
    if not diagnostics["bearer_scheme"]:
        return "invalid_or_missing_bearer_token"
    if not diagnostics["mcp_prefix"]:
        return "non_mcp_credential_bearer"

    if "required" in detail.lower():
        return "authentication_required"
    if "invalid, expired, or revoked" in detail.lower():
        return "credential_invalid_expired_or_revoked"
    if "inactive" in detail.lower():
        return "employee_inactive"

    return "credential_validation_failed"


def execution_context_for_request(
    request: Request | None,
) -> MCPExecutionContext:
    if request is None:
        raise MCPAuthenticationError("Authentication required.")

    cached = getattr(request.state, "mcp_context", None)

    if isinstance(cached, MCPExecutionContext):
        return cached

    headers = request.headers
    diagnostics = _authorization_diagnostics(headers)
    token = bearer_token_from_headers(headers)

    try:
        context = execution_context_from_token(token)
    except MCPAuthenticationError as error:
        logger.warning(
            "MCP auth rejected: reason=%s authorization_present=%s bearer_scheme=%s mcp_prefix=%s path=%s method=%s",
            _rejection_reason(headers, error),
            diagnostics["authorization_present"],
            diagnostics["bearer_scheme"],
            diagnostics["mcp_prefix"],
            getattr(request.url, "path", None),
            getattr(request, "method", None),
        )
        raise

    request.state.mcp_context = context
    return context
