"""OAuth endpoints for remote MCP clients (ChatGPT private workspace)."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import auth.config as auth_config
from auth.mcp_oauth import (
    MCPOAuthError,
    authorization_server_metadata,
    build_google_return_to,
    canonical_issuer,
    canonical_mcp_resource,
    exchange_authorization_code,
    issue_authorization_code,
    parse_authorization_request,
    refresh_access_token,
    register_oauth_client,
    revoke_token,
)
from auth.routes import _client_details, _cookie_options
from auth.security import create_state_token, decode_state_token, pkce_pair, random_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-oauth"])


@router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server_metadata(request: Request):
    settings = auth_config.settings
    issuer = canonical_issuer(settings)
    resource = canonical_mcp_resource(settings)
    return JSONResponse(authorization_server_metadata(issuer=issuer, resource=resource))


@router.get("/oauth/authorize")
def oauth_authorize(request: Request):
    settings = auth_config.settings
    if not settings.google_configured:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")

    try:
        mcp_request = parse_authorization_request(dict(request.query_params), settings=settings)
    except MCPOAuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.description)

    verifier, challenge = pkce_pair()
    google_state = random_token(32)
    nonce = random_token(32)
    return_to = build_google_return_to(mcp_request)
    state_token = create_state_token(
        state=google_state,
        nonce=nonce,
        code_verifier=verifier,
        return_to=return_to,
        settings=settings,
        extra={
            "mcp_oauth": True,
            "mcp": {
                "client_id": mcp_request.client_id,
                "redirect_uri": mcp_request.redirect_uri,
                "state": mcp_request.state,
                "scope": mcp_request.scope,
                "code_challenge": mcp_request.code_challenge,
                "code_challenge_method": mcp_request.code_challenge_method,
                "resource": mcp_request.resource,
            },
        },
    )

    from auth.google import authorization_url

    auth_url = authorization_url(
        state=google_state,
        nonce=nonce,
        code_challenge=challenge,
        settings=settings,
    )
    response = RedirectResponse(auth_url, status_code=302)
    response.set_cookie(
        settings.oauth_state_cookie_name,
        state_token,
        max_age=600,
        **_cookie_options(settings, httponly=True),
    )
    return response


@router.get("/auth/mcp/callback")
def oauth_mcp_callback(request: Request):
    """Internal callback target after Google login for MCP OAuth requests."""
    settings = auth_config.settings
    params = dict(request.query_params)
    error = params.get("error")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    if error:
        if redirect_uri and state:
            q = urlencode({"error": "access_denied", "state": state})
            return RedirectResponse(f"{redirect_uri}?{q}", status_code=302)
        raise HTTPException(status_code=400, detail="OAuth authorization failed.")

    employee_id = request.session.get("mcp_oauth_employee_id") if hasattr(request, "session") else None
    # We rely on signed state cookie flow through Google callback route; this endpoint
    # is reached only as return_to after successful callback and is protected below.
    # Actual issuance is handled in auth.routes.google_callback when mcp_oauth flag present.
    raise HTTPException(status_code=400, detail="Invalid MCP OAuth callback.")


@router.post("/oauth/register")
async def oauth_register(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "invalid_client_metadata", "error_description": "JSON object required."})
    try:
        result = register_oauth_client(payload, settings=auth_config.settings)
    except MCPOAuthError as error:
        return JSONResponse(status_code=error.status_code, content={"error": error.error, "error_description": error.description})
    return JSONResponse(status_code=201, content=result)


@router.post("/oauth/token")
async def oauth_token(request: Request):
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    try:
        if grant_type == "authorization_code":
            payload = exchange_authorization_code(
                code=str(form.get("code") or ""),
                redirect_uri=str(form.get("redirect_uri") or ""),
                client_id=str(form.get("client_id") or ""),
                code_verifier=str(form.get("code_verifier") or ""),
                resource=str(form.get("resource") or "") or None,
                settings=auth_config.settings,
            )
            return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if grant_type == "refresh_token":
            payload = refresh_access_token(
                refresh_token=str(form.get("refresh_token") or ""),
                client_id=str(form.get("client_id") or "") or None,
                resource=str(form.get("resource") or "") or None,
                settings=auth_config.settings,
            )
            return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        raise MCPOAuthError("unsupported_grant_type", "Unsupported grant_type.")
    except MCPOAuthError as error:
        logger.warning("MCP OAuth token error: %s", error.error)
        return JSONResponse(
            {"error": error.error, "error_description": error.description},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


@router.post("/oauth/revoke")
async def oauth_revoke(request: Request):
    form = await request.form()
    token = str(form.get("token") or "")
    hint = str(form.get("token_type_hint") or "") or None
    if token:
        revoke_token(token=token, token_type_hint=hint)
    return JSONResponse({}, status_code=200)
