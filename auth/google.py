"""Google OpenID Connect Authorization Code flow with PKCE."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from auth.config import AuthSettings


class GoogleAuthError(Exception):
    """A safe, user-facing Google authentication failure."""


def authorization_url(
    *,
    state: str,
    nonce: str,
    code_challenge: str,
    settings: AuthSettings,
) -> str:
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
    )

    return f"{settings.google_authorization_endpoint}?{query}"


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    settings: AuthSettings,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }

    try:
        if client is None:
            with httpx.Client(timeout=10.0) as temporary:
                response = temporary.post(settings.google_token_endpoint, data=data)
        else:
            response = client.post(settings.google_token_endpoint, data=data)

        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise GoogleAuthError("Google sign-in could not be completed.") from error

    if not isinstance(payload, dict) or not payload.get("id_token"):
        raise GoogleAuthError("Google did not return an identity token.")

    return payload


def verify_id_token(
    *,
    id_token: str,
    nonce: str,
    settings: AuthSettings,
    jwks_client: jwt.PyJWKClient | None = None,
) -> dict[str, Any]:
    try:
        client = jwks_client or jwt.PyJWKClient(settings.google_jwks_uri)
        signing_key = client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=settings.google_issuers,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=30,
        )
    except jwt.PyJWTError as error:
        raise GoogleAuthError("Google returned an invalid identity token.") from error

    if not claims.get("nonce") or claims.get("nonce") != nonce:
        raise GoogleAuthError("Google sign-in verification failed.")

    email_verified = claims.get("email_verified")

    if email_verified not in (True, "true", "True", 1):
        raise GoogleAuthError("A verified Google email address is required.")

    if not claims.get("email") or not claims.get("sub"):
        raise GoogleAuthError("Google did not return a usable account.")

    return claims
