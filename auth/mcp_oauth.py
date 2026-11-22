"""OAuth 2.1 authorization server for private ChatGPT MCP integration.

This module is intentionally narrow: it supports authorization code + PKCE,
refresh tokens, revocation, and protected-resource/authorization-server
metadata required by remote MCP clients. It reuses the existing Google sign-in
identity flow and existing employee/RBAC model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from sqlalchemy.orm import joinedload

from auth import config as auth_config
from auth.config import AuthSettings
from auth.security import (
    create_mcp_oauth_access_token,
    decode_mcp_oauth_access_token,
    random_token,
    token_hash,
    utcnow,
)
from database.connection import SessionLocal
from database.models import Employee, MCPOAuthClient, MCPOAuthCode, MCPOAuthSession, Role

logger = logging.getLogger(__name__)

DEFAULT_MCP_SCOPE = "mcp:tools"
SUPPORTED_SCOPES = {DEFAULT_MCP_SCOPE}
SUPPORTED_CODE_CHALLENGE_METHODS = {"S256"}
SUPPORTED_GRANT_TYPES = {"authorization_code", "refresh_token"}
SUPPORTED_RESPONSE_TYPES = {"code"}
SUPPORTED_TOKEN_AUTH_METHODS = {"none"}
MCP_OAUTH_CODE_TTL_SECONDS = 5 * 60
MCP_OAUTH_REFRESH_DAYS = 30
MCP_OAUTH_ACCESS_MINUTES = 60
CANONICAL_MCP_RESOURCE = "https://fnw-logging-assistant.onrender.com/mcp"


class MCPOAuthError(Exception):
    def __init__(self, error: str, description: str, status_code: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


@dataclass(frozen=True)
class MCPAuthorizationRequest:
    response_type: str
    client_id: str
    redirect_uri: str
    state: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    resource: str | None


def normalize_scope(scope: str | None) -> str:
    values = [v for v in (scope or DEFAULT_MCP_SCOPE).split() if v]
    if not values:
        values = [DEFAULT_MCP_SCOPE]
    unknown = [v for v in values if v not in SUPPORTED_SCOPES]
    if unknown:
        raise MCPOAuthError("invalid_scope", "Unsupported scope requested.")
    # Stable order
    return " ".join(sorted(set(values)))


def _is_https(uri: str) -> bool:
    return urlparse(uri).scheme == "https"


def _is_loopback_http(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


def canonical_mcp_resource(settings: AuthSettings | None = None) -> str:
    settings = settings or auth_config.settings
    try:
        from mcp_server.configuration import load_mcp_settings
        from mcp_server.server import _normalize_mcp_path

        mcp = load_mcp_settings()
        configured = (mcp.public_resource_url or "").strip()
        if configured:
            # public_resource_url is canonicalized to include /mcp path
            return configured.rstrip("/")
        # fallback to issuer-less canonical constant
        return CANONICAL_MCP_RESOURCE
    except Exception:
        return CANONICAL_MCP_RESOURCE


def canonical_issuer(settings: AuthSettings | None = None) -> str:
    resource = canonical_mcp_resource(settings)
    parsed = urlparse(resource)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def validate_redirect_uri(redirect_uri: str) -> str:
    value = (redirect_uri or "").strip()
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise MCPOAuthError("invalid_request", "redirect_uri must be an absolute URI.")
    if parsed.fragment:
        raise MCPOAuthError("invalid_request", "redirect_uri must not include a fragment.")
    if not (_is_https(value) or _is_loopback_http(value)):
        raise MCPOAuthError("invalid_request", "redirect_uri must use HTTPS or loopback HTTP.")
    return value


def validate_redirect_uris(redirect_uris: Any) -> list[str]:
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise MCPOAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty array.")
    validated = []
    for uri in redirect_uris:
        if not isinstance(uri, str):
            raise MCPOAuthError("invalid_redirect_uri", "redirect_uris must contain strings.")
        validated.append(validate_redirect_uri(uri))
    # de-dupe preserving order
    out = []
    for uri in validated:
        if uri not in out:
            out.append(uri)
    return out


def validate_client_id(client_id: str) -> str:
    value = (client_id or "").strip()
    if not value or len(value) > 255:
        raise MCPOAuthError("invalid_request", "Invalid client_id.")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
    if any(ch not in allowed for ch in value):
        raise MCPOAuthError("invalid_request", "Invalid client_id.")
    return value


def get_registered_client(client_id: str) -> MCPOAuthClient | None:
    session = SessionLocal()
    try:
        return session.query(MCPOAuthClient).filter(MCPOAuthClient.client_id == client_id).first()
    finally:
        session.close()


def require_registered_client(client_id: str) -> MCPOAuthClient:
    client = get_registered_client(validate_client_id(client_id))
    if client is None:
        raise MCPOAuthError("invalid_client", "Unknown client_id.", 401)
    return client


def validate_resource(resource: str | None, *, settings: AuthSettings | None = None, required: bool = True) -> str:
    expected = canonical_mcp_resource(settings).rstrip("/")
    value = (resource or "").strip().rstrip("/")
    if not value:
        if required:
            raise MCPOAuthError("invalid_target", "resource is required.")
        return expected
    if value != expected:
        raise MCPOAuthError("invalid_target", "resource mismatch.")
    return value


def register_oauth_client(payload: dict[str, Any], *, settings: AuthSettings | None = None) -> dict[str, Any]:
    settings = settings or auth_config.settings
    redirect_uris = validate_redirect_uris(payload.get("redirect_uris"))
    token_auth_method = str(payload.get("token_endpoint_auth_method") or "none").strip()
    if token_auth_method not in SUPPORTED_TOKEN_AUTH_METHODS:
        raise MCPOAuthError("invalid_client_metadata", "Only token_endpoint_auth_method=none is supported.")
    grant_types = payload.get("grant_types") or ["authorization_code", "refresh_token"]
    response_types = payload.get("response_types") or ["code"]
    if not isinstance(grant_types, list) or set(grant_types) != SUPPORTED_GRANT_TYPES:
        raise MCPOAuthError("invalid_client_metadata", "grant_types must include authorization_code and refresh_token.")
    if not isinstance(response_types, list) or set(response_types) != SUPPORTED_RESPONSE_TYPES:
        raise MCPOAuthError("invalid_client_metadata", "response_types must be [\"code\"].")
    scope = normalize_scope(payload.get("scope"))
    client_name = str(payload.get("client_name") or "").strip()[:255] or None
    client_uri = str(payload.get("client_uri") or "").strip() or None
    if client_uri and not (_is_https(client_uri) or _is_loopback_http(client_uri)):
        raise MCPOAuthError("invalid_client_metadata", "client_uri must be absolute HTTPS/loopback URI.")

    client_id = secrets.token_urlsafe(32)
    row = MCPOAuthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=redirect_uris,
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        client_uri=client_uri,
    )
    session = SessionLocal()
    try:
        session.add(row)
        session.commit()
        created_at = row.created_at
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": scope,
        "client_id_issued_at": int(created_at.timestamp()) if created_at else int(utcnow().timestamp()),
    }


def parse_authorization_request(args: dict[str, Any], *, settings: AuthSettings | None = None) -> MCPAuthorizationRequest:
    settings = settings or auth_config.settings
    response_type = str(args.get("response_type") or "").strip()
    client_id = validate_client_id(str(args.get("client_id") or ""))
    redirect_uri = validate_redirect_uri(str(args.get("redirect_uri") or ""))
    state = str(args.get("state") or "").strip()
    scope = normalize_scope(args.get("scope"))
    code_challenge = str(args.get("code_challenge") or "").strip()
    code_challenge_method = str(args.get("code_challenge_method") or "S256").strip()
    resource = validate_resource(str(args.get("resource") or "").strip() or None, settings=settings, required=True)

    client = require_registered_client(client_id)
    if redirect_uri not in (client.redirect_uris or []):
        raise MCPOAuthError("invalid_request", "redirect_uri is not registered for this client.")

    if response_type != "code":
        raise MCPOAuthError("unsupported_response_type", "Only response_type=code is supported.")
    if not state:
        raise MCPOAuthError("invalid_request", "state is required.")
    if not code_challenge:
        raise MCPOAuthError("invalid_request", "code_challenge is required.")
    if code_challenge_method not in SUPPORTED_CODE_CHALLENGE_METHODS:
        raise MCPOAuthError("invalid_request", "Only S256 PKCE is supported.")

    return MCPAuthorizationRequest(
        response_type=response_type,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        resource=resource,
    )


def build_google_return_to(request: MCPAuthorizationRequest) -> str:
    # Internal callback route will issue code and redirect to external redirect_uri.
    query = urlencode(
        {
            "mcp_oauth": "1",
            "client_id": request.client_id,
            "redirect_uri": request.redirect_uri,
            "state": request.state,
            "scope": request.scope,
            "code_challenge": request.code_challenge,
            "code_challenge_method": request.code_challenge_method,
            **({"resource": request.resource} if request.resource else {}),
        }
    )
    return f"/auth/mcp/callback?{query}"


def _employee_identity(employee: Employee) -> dict[str, Any]:
    role = employee.role
    permissions = sorted({p.name for p in (role.permissions or [])}) if role else []
    return {
        "id": int(employee.id),
        "email": employee.email,
        "role": role.name if role else "",
        "permissions": permissions,
        "is_active": bool(employee.is_active),
    }


def issue_authorization_code(
    *,
    employee_id: int,
    request: MCPAuthorizationRequest,
    settings: AuthSettings | None = None,
) -> str:
    settings = settings or default_settings
    code = random_token(48)
    now = utcnow()
    row = MCPOAuthCode(
        id=str(uuid.uuid4()),
        employee_id=int(employee_id),
        client_id=request.client_id,
        code_hash=token_hash(code),
        redirect_uri=request.redirect_uri,
        scope=request.scope,
        code_challenge=request.code_challenge,
        code_challenge_method=request.code_challenge_method,
        resource=request.resource,
        expires_at=now + timedelta(seconds=MCP_OAUTH_CODE_TTL_SECONDS),
        created_at=now,
    )
    session = SessionLocal()
    try:
        session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return code


def _pkce_matches(verifier: str, challenge: str, method: str) -> bool:
    if not verifier or not challenge:
        return False
    if method != "S256":
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def _load_active_employee(session, employee_id: int) -> Employee | None:
    return (
        session.query(Employee)
        .options(joinedload(Employee.role).selectinload(Role.permissions))
        .filter(Employee.id == int(employee_id))
        .first()
    )


def exchange_authorization_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    resource: str | None = None,
    settings: AuthSettings | None = None,
) -> dict[str, Any]:
    settings = settings or auth_config.settings
    now = utcnow()
    session = SessionLocal()
    try:
        row = (
            session.query(MCPOAuthCode)
            .filter(MCPOAuthCode.code_hash == token_hash(code or ""))
            .first()
        )
        if row is None:
            raise MCPOAuthError("invalid_grant", "Authorization code is invalid.")
        if row.revoked_at is not None or row.used_at is not None:
            raise MCPOAuthError("invalid_grant", "Authorization code has already been used.")
        if row.expires_at <= now:
            raise MCPOAuthError("invalid_grant", "Authorization code has expired.")
        if not hmac.compare_digest(row.client_id, client_id or ""):
            raise MCPOAuthError("invalid_grant", "client_id mismatch.")
        client = session.query(MCPOAuthClient).filter(MCPOAuthClient.client_id == row.client_id).first()
        if client is None:
            raise MCPOAuthError("invalid_client", "Unknown client_id.", 401)
        if redirect_uri not in (client.redirect_uris or []) or not hmac.compare_digest(row.redirect_uri, redirect_uri or ""):
            raise MCPOAuthError("invalid_grant", "redirect_uri mismatch.")
        if not _pkce_matches(code_verifier or "", row.code_challenge, row.code_challenge_method):
            raise MCPOAuthError("invalid_grant", "PKCE verification failed.")
        expected_resource = validate_resource(resource or row.resource, settings=settings, required=True)
        if (row.resource or "").rstrip("/") != expected_resource.rstrip("/"):
            raise MCPOAuthError("invalid_target", "resource mismatch.")

        employee = _load_active_employee(session, row.employee_id)
        if employee is None or not employee.is_active:
            raise MCPOAuthError("invalid_grant", "Employee is not authorized.")

        row.used_at = now
        oauth_session = MCPOAuthSession(
            id=str(uuid.uuid4()),
            employee_id=int(employee.id),
            client_id=row.client_id,
            scope=row.scope,
            resource=row.resource,
            refresh_token_hash="",  # filled below
            expires_at=now + timedelta(days=MCP_OAUTH_REFRESH_DAYS),
            created_at=now,
            last_used_at=now,
        )
        refresh_token = random_token(48)
        oauth_session.refresh_token_hash = token_hash(refresh_token)
        session.add(oauth_session)
        session.flush()

        identity = _employee_identity(employee)
        access_token, access_expires, jti = create_mcp_oauth_access_token(
            employee_id=identity["id"],
            email=identity["email"],
            role=identity["role"],
            permissions=identity["permissions"],
            session_id=oauth_session.id,
            client_id=row.client_id,
            scope=row.scope or DEFAULT_MCP_SCOPE,
            resource=expected_resource,
            settings=settings,
            now=now,
            ttl_minutes=MCP_OAUTH_ACCESS_MINUTES,
        )
        oauth_session.access_jti = jti
        session.commit()
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int((access_expires - now).total_seconds()),
            "refresh_token": refresh_token,
            "scope": row.scope or DEFAULT_MCP_SCOPE,
            "resource": expected_resource,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def refresh_access_token(
    *,
    refresh_token: str,
    client_id: str | None = None,
    resource: str | None = None,
    settings: AuthSettings | None = None,
) -> dict[str, Any]:
    settings = settings or auth_config.settings
    now = utcnow()
    session = SessionLocal()
    try:
        row = (
            session.query(MCPOAuthSession)
            .filter(MCPOAuthSession.refresh_token_hash == token_hash(refresh_token or ""))
            .first()
        )
        if row is None:
            raise MCPOAuthError("invalid_grant", "Refresh token is invalid.")
        if client_id and not hmac.compare_digest(row.client_id, client_id):
            raise MCPOAuthError("invalid_grant", "client_id mismatch.")
        if row.revoked_at is not None:
            # replay/reuse detection: revoke replacement chain
            session.query(MCPOAuthSession).filter(
                MCPOAuthSession.employee_id == row.employee_id,
                MCPOAuthSession.client_id == row.client_id,
                MCPOAuthSession.revoked_at.is_(None),
            ).update({MCPOAuthSession.revoked_at: now}, synchronize_session=False)
            session.commit()
            raise MCPOAuthError("invalid_grant", "Refresh token has been revoked.")
        if row.expires_at <= now:
            raise MCPOAuthError("invalid_grant", "Refresh token has expired.")

        employee = _load_active_employee(session, row.employee_id)
        if employee is None or not employee.is_active:
            row.revoked_at = now
            session.commit()
            raise MCPOAuthError("invalid_grant", "Employee is not authorized.")

        # Rotate refresh token
        row.revoked_at = now
        row.last_used_at = now
        new_refresh = random_token(48)
        new_row = MCPOAuthSession(
            id=str(uuid.uuid4()),
            employee_id=row.employee_id,
            client_id=row.client_id,
            scope=row.scope,
            resource=row.resource,
            refresh_token_hash=token_hash(new_refresh),
            expires_at=now + timedelta(days=MCP_OAUTH_REFRESH_DAYS),
            created_at=now,
            last_used_at=now,
        )
        session.add(new_row)
        session.flush()
        row.replaced_by_session_id = new_row.id

        identity = _employee_identity(employee)
        expected_resource = validate_resource(resource or row.resource, settings=settings, required=True)
        if (row.resource or "").rstrip("/") != expected_resource.rstrip("/"):
            raise MCPOAuthError("invalid_target", "resource mismatch.")
        access_token, access_expires, jti = create_mcp_oauth_access_token(
            employee_id=identity["id"],
            email=identity["email"],
            role=identity["role"],
            permissions=identity["permissions"],
            session_id=new_row.id,
            client_id=row.client_id,
            scope=row.scope or DEFAULT_MCP_SCOPE,
            resource=expected_resource,
            settings=settings,
            now=now,
            ttl_minutes=MCP_OAUTH_ACCESS_MINUTES,
        )
        new_row.access_jti = jti
        session.commit()
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int((access_expires - now).total_seconds()),
            "refresh_token": new_refresh,
            "scope": row.scope or DEFAULT_MCP_SCOPE,
            "resource": expected_resource,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def revoke_token(*, token: str, token_type_hint: str | None = None) -> None:
    now = utcnow()
    session = SessionLocal()
    try:
        digest = token_hash(token or "")
        row = (
            session.query(MCPOAuthSession)
            .filter(MCPOAuthSession.refresh_token_hash == digest)
            .first()
        )
        if row is not None and row.revoked_at is None:
            row.revoked_at = now
            session.commit()
            return

        # Access tokens are short-lived; revocation endpoint can be best-effort.
        # If token is an MCP OAuth access token, revoke its backing session.
        try:
            payload = decode_mcp_oauth_access_token(token, auth_config.settings)
            sid = str(payload.get("sid") or "")
            if sid:
                sess = session.query(MCPOAuthSession).filter(MCPOAuthSession.id == sid).first()
                if sess is not None and sess.revoked_at is None:
                    sess.revoked_at = now
                    session.commit()
        except Exception:
            session.rollback()
            return
    finally:
        session.close()


def validate_mcp_oauth_access_token(token: str, *, settings: AuthSettings | None = None, expected_resource: str | None = None):
    from auth.service import AuthenticatedEmployee

    settings = settings or auth_config.settings
    expected = (expected_resource or canonical_mcp_resource(settings)).rstrip("/")
    payload = decode_mcp_oauth_access_token(token, settings, audience=expected)
    session_id = str(payload.get("sid") or "")
    if not session_id:
        raise MCPOAuthError("invalid_token", "Invalid token.", 401)

    session = SessionLocal()
    try:
        oauth_session = (
            session.query(MCPOAuthSession)
            .options(joinedload(MCPOAuthSession.employee).joinedload(Employee.role).selectinload(Role.permissions))
            .filter(MCPOAuthSession.id == session_id)
            .first()
        )
        if oauth_session is None or oauth_session.revoked_at is not None or oauth_session.expires_at <= utcnow():
            raise MCPOAuthError("invalid_token", "Token session is no longer active.", 401)
        if (oauth_session.resource or "").rstrip("/") != expected:
            raise MCPOAuthError("invalid_token", "Token resource mismatch.", 401)
        claim_resource = str(payload.get("resource") or "").rstrip("/")
        if claim_resource != expected:
            raise MCPOAuthError("invalid_token", "Token resource mismatch.", 401)
        employee = oauth_session.employee
        if employee is None or not employee.is_active:
            raise MCPOAuthError("invalid_token", "Employee is not authorized.", 401)
        role = employee.role
        permissions = frozenset(p.name for p in (role.permissions or [])) if role else frozenset()
        identity = AuthenticatedEmployee(
            id=int(employee.id),
            name=employee.name,
            email=employee.email,
            role=role.name if role else "",
            permissions=permissions,
            session_id=session_id,
        )
        return identity, session_id
    finally:
        session.close()


def authorization_server_metadata(*, issuer: str, resource: str | None = None) -> dict[str, Any]:
    base = issuer.rstrip("/")
    resource = (resource or canonical_mcp_resource()).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": sorted(SUPPORTED_SCOPES),
        "authorization_response_iss_parameter_supported": True,
        "resource": resource,
    }
