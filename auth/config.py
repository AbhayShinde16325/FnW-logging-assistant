"""Environment-backed authentication settings.

The values are loaded once for normal runtime use. ``load_auth_settings`` is
kept public so tests and deployment checks can build a settings object from a
known environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    values: list[str] = []

    for item in raw.split(","):
        value = item.strip().rstrip("/")

        if value and value not in values:
            values.append(value)

    return tuple(values)


@dataclass(frozen=True)
class AuthSettings:
    required: bool
    secret_key: str
    issuer: str
    audience: str
    access_token_minutes: int
    refresh_token_days: int
    cookie_secure: bool
    cookie_same_site: str
    cookie_domain: str | None
    refresh_cookie_name: str
    csrf_cookie_name: str
    oauth_state_cookie_name: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_authorization_endpoint: str
    google_token_endpoint: str
    google_jwks_uri: str
    google_issuers: tuple[str, ...]
    post_login_redirect: str
    frontend_origins: tuple[str, ...]
    employee_domain: str | None
    bootstrap_admin_email: str | None
    bootstrap_admin_name: str

    @property
    def frontend_origin(self) -> str:
        """Primary frontend origin for backward-compatible callers."""

        return self.frontend_origins[0]

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def secure_config_errors(self) -> list[str]:
        errors = []

        if self.required and len(self.secret_key) < 32:
            errors.append(
                "AUTH_SECRET_KEY must be at least 32 characters when "
                "AUTH_REQUIRED is enabled."
            )

        if self.required and not self.google_configured:
            errors.append(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required when "
                "AUTH_REQUIRED is enabled."
            )

        parsed = urlparse(self.google_redirect_uri)

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("GOOGLE_REDIRECT_URI must be an absolute URL.")

        for origin in self.frontend_origins:
            origin_parsed = urlparse(origin)
            if origin_parsed.scheme not in {"http", "https"} or not origin_parsed.netloc:
                errors.append(f"FRONTEND_URL entry must be an absolute origin: {origin}")

        post_login = urlparse(self.post_login_redirect)
        if post_login.scheme not in {"http", "https"} or not post_login.netloc:
            errors.append("AUTH_POST_LOGIN_REDIRECT must be an absolute URL.")
        else:
            post_login_origin = f"{post_login.scheme}://{post_login.netloc}".rstrip("/")
            if post_login_origin not in self.frontend_origins:
                errors.append(
                    "AUTH_POST_LOGIN_REDIRECT must match an allowed FRONTEND_URL origin."
                )

        if self.cookie_secure and parsed.scheme != "https":
            # This is a warning-level deployment issue rather than a startup
            # failure: local HTTP development is useful and explicit.
            pass

        return errors


def load_auth_settings() -> AuthSettings:
    frontend_origins = _csv("FRONTEND_URL") or ("http://localhost:8080",)
    primary_frontend_origin = frontend_origins[0]
    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        f"{primary_frontend_origin}/auth/google/callback",
    )
    post_login_redirect = os.getenv(
        "AUTH_POST_LOGIN_REDIRECT", primary_frontend_origin
    ).strip().rstrip("/")

    # Safe fallback: default redirect must target an allowed frontend origin.
    post_login_origin = urlparse(post_login_redirect)
    post_login_origin = f"{post_login_origin.scheme}://{post_login_origin.netloc}".rstrip("/")
    if post_login_origin not in frontend_origins:
        post_login_redirect = primary_frontend_origin

    return AuthSettings(
        required=_bool("AUTH_REQUIRED", True),
        secret_key=os.getenv("AUTH_SECRET_KEY", "").strip(),
        issuer=os.getenv("AUTH_TOKEN_ISSUER", "task-flow-automation").strip(),
        audience=os.getenv("AUTH_TOKEN_AUDIENCE", "task-flow-web").strip(),
        access_token_minutes=max(5, min(_int("ACCESS_TOKEN_MINUTES", 15), 60)),
        refresh_token_days=max(1, min(_int("REFRESH_TOKEN_DAYS", 14), 30)),
        cookie_secure=_bool("AUTH_COOKIE_SECURE", True),
        cookie_same_site=os.getenv("AUTH_COOKIE_SAME_SITE", "lax").strip().lower(),
        cookie_domain=os.getenv("AUTH_COOKIE_DOMAIN", "").strip() or None,
        refresh_cookie_name=os.getenv(
            "AUTH_REFRESH_COOKIE", "taskflow_refresh"
        ).strip(),
        csrf_cookie_name=os.getenv(
            "AUTH_CSRF_COOKIE", "taskflow_csrf"
        ).strip(),
        oauth_state_cookie_name=os.getenv(
            "AUTH_OAUTH_STATE_COOKIE", "taskflow_oauth_state"
        ).strip(),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        google_redirect_uri=redirect_uri.strip(),
        google_authorization_endpoint=os.getenv(
            "GOOGLE_AUTHORIZATION_ENDPOINT",
            "https://accounts.google.com/o/oauth2/v2/auth",
        ).strip(),
        google_token_endpoint=os.getenv(
            "GOOGLE_TOKEN_ENDPOINT",
            "https://oauth2.googleapis.com/token",
        ).strip(),
        google_jwks_uri=os.getenv(
            "GOOGLE_JWKS_URI",
            "https://www.googleapis.com/oauth2/v3/certs",
        ).strip(),
        google_issuers=("accounts.google.com", "https://accounts.google.com"),
        post_login_redirect=post_login_redirect,
        frontend_origins=frontend_origins,
        employee_domain=(
            os.getenv("AUTH_ALLOWED_EMAIL_DOMAIN", "").strip().lower() or None
        ),
        bootstrap_admin_email=(
            os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower() or None
        ),
        bootstrap_admin_name=(
            os.getenv("BOOTSTRAP_ADMIN_NAME", "Administrator").strip()
            or "Administrator"
        ),
    )


settings = load_auth_settings()
