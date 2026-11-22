"""Token, PKCE, CSRF and hash helpers for authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import jwt

from auth.config import AuthSettings

ALGORITHM = "HS256"
STATE_TYPE = "oauth_state"
ACCESS_TYPE = "access"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def random_token(size: int = 48) -> str:
    return secrets.token_urlsafe(size)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False

    return hmac.compare_digest(left, right)


def _require_secret(settings: AuthSettings) -> str:
    if len(settings.secret_key) < 32:
        raise RuntimeError(
            "AUTH_SECRET_KEY must be configured with at least 32 characters."
        )

    return settings.secret_key


def create_access_token(
    *,
    employee_id: int,
    email: str,
    role: str,
    permissions: list[str] | set[str],
    session_id: str,
    settings: AuthSettings,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued_at = now or utcnow()
    expires_at = issued_at + timedelta(minutes=settings.access_token_minutes)

    payload = {
        "sub": str(employee_id),
        "email": email,
        "role": role,
        "permissions": sorted(permissions),
        "sid": session_id,
        "type": ACCESS_TYPE,
        "iss": settings.issuer,
        "aud": settings.audience,
        "iat": issued_at.replace(tzinfo=timezone.utc),
        "nbf": issued_at.replace(tzinfo=timezone.utc),
        "exp": expires_at.replace(tzinfo=timezone.utc),
        "jti": secrets.token_urlsafe(16),
    }

    token = jwt.encode(payload, _require_secret(settings), algorithm=ALGORITHM)
    return token, expires_at


def decode_access_token(token: str, settings: AuthSettings) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        _require_secret(settings),
        algorithms=[ALGORITHM],
        audience=settings.audience,
        issuer=settings.issuer,
        options={"require": ["exp", "iat", "sub", "iss", "aud", "sid"]},
        leeway=10,
    )

    if payload.get("type") != ACCESS_TYPE:
        raise jwt.InvalidTokenError("Unexpected token type.")

    return payload


def create_state_token(
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    return_to: str,
    settings: AuthSettings,
    now: datetime | None = None,
) -> str:
    issued_at = now or utcnow()

    payload = {
        "type": STATE_TYPE,
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "return_to": return_to,
        "iat": issued_at.replace(tzinfo=timezone.utc),
        "exp": (issued_at + timedelta(minutes=10)).replace(tzinfo=timezone.utc),
    }

    return jwt.encode(payload, _require_secret(settings), algorithm=ALGORITHM)


def decode_state_token(token: str, settings: AuthSettings) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        _require_secret(settings),
        algorithms=[ALGORITHM],
        options={"require": ["exp", "iat", "state", "nonce", "code_verifier"]},
        leeway=10,
    )

    if payload.get("type") != STATE_TYPE:
        raise jwt.InvalidTokenError("Unexpected state token type.")

    return payload


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _normalize_allowed_origins(
    allowed_origins: str | list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    if isinstance(allowed_origins, str):
        raw_values = allowed_origins.split(",")
    else:
        raw_values = allowed_origins

    origins: list[str] = []

    for value in raw_values:
        origin = str(value or "").strip().rstrip("/")

        if origin and origin not in origins:
            origins.append(origin)

    return tuple(origins)


def safe_return_to(
    value: str | None,
    default: str,
    allowed_origins: str | list[str] | tuple[str, ...],
) -> str:
    """Return a trusted post-auth redirect target.

    Absolute redirects are allowed only when their origin is explicitly allowed.
    Relative paths are resolved against the default redirect's origin when that
    origin is allowed; otherwise they fall back to the primary allowed origin.
    """

    origins = _normalize_allowed_origins(allowed_origins)

    if not origins:
        return default

    default_value = (default or "").strip() or origins[0]
    default_parsed = urlparse(default_value)
    default_origin = f"{default_parsed.scheme}://{default_parsed.netloc}".rstrip("/")
    fallback_origin = default_origin if default_origin in origins else origins[0]

    if not value:
        return default_value if default_origin in origins else fallback_origin

    candidate = value.strip()

    if candidate.startswith("/") and not candidate.startswith("//"):
        return f"{fallback_origin}{candidate}"

    parsed = urlparse(candidate)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return default_value if default_origin in origins else fallback_origin

    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    if origin not in origins:
        return default_value if default_origin in origins else fallback_origin

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"

    target = f"{origin}{path}"

    if parsed.query:
        target = f"{target}?{parsed.query}"

    if parsed.fragment:
        target = f"{target}#{parsed.fragment}"

    return target
