#!/usr/bin/env python3
"""Safe OAuth configuration diagnostic. Never prints secrets/tokens/codes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import auth.config as auth_config

try:
    from app import app
except Exception:
    app = None

from auth.routes import router as auth_router


def _csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


EXPECTED_CLIENT_ID = os.getenv("EXPECTED_GOOGLE_CLIENT_ID", "").strip()
EXPECTED_REDIRECT_URI = os.getenv("EXPECTED_GOOGLE_REDIRECT_URI", "").strip()
EXPECTED_FRONTEND_ORIGINS = _csv_env("EXPECTED_FRONTEND_URLS")
EXPECTED_POST_LOGIN = os.getenv("EXPECTED_AUTH_POST_LOGIN_REDIRECT", "").strip().rstrip("/")
EXPECTED_AUTH_ENDPOINT = os.getenv(
    "EXPECTED_GOOGLE_AUTHORIZATION_ENDPOINT",
    "https://accounts.google.com/o/oauth2/v2/auth",
).strip()
EXPECTED_TOKEN_ENDPOINT = os.getenv(
    "EXPECTED_GOOGLE_TOKEN_ENDPOINT",
    "https://oauth2.googleapis.com/token",
).strip()


def ok(label, value, good=True):
    status = "OK" if good else "FAIL"
    print(f"[{status}] {label}: {value}")
    return 0 if good else 1


def valid_abs_url(value: str) -> bool:
    p = urlparse(value)
    return p.scheme in {"http", "https"} and bool(p.netloc)


def route_exists(path: str, method: str = "GET") -> bool:
    routes = []
    if app is not None:
        routes.extend(app.routes)
    routes.extend(auth_router.routes)
    for route in routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return True
    return False


def main() -> int:
    s = auth_config.load_auth_settings()
    failures = 0

    failures += ok("GOOGLE_CLIENT_ID present", bool(s.google_client_id), bool(s.google_client_id))
    if EXPECTED_CLIENT_ID:
        failures += ok(
            "GOOGLE_CLIENT_ID matches expected",
            s.google_client_id == EXPECTED_CLIENT_ID,
            s.google_client_id == EXPECTED_CLIENT_ID,
        )
    else:
        print("[INFO] EXPECTED_GOOGLE_CLIENT_ID not set; skipping exact client ID match.")

    failures += ok("GOOGLE_CLIENT_SECRET present", bool(s.google_client_secret), bool(s.google_client_secret))
    failures += ok("GOOGLE_REDIRECT_URI valid", valid_abs_url(s.google_redirect_uri), valid_abs_url(s.google_redirect_uri))
    if EXPECTED_REDIRECT_URI:
        failures += ok(
            "GOOGLE_REDIRECT_URI matches expected",
            s.google_redirect_uri == EXPECTED_REDIRECT_URI,
            s.google_redirect_uri == EXPECTED_REDIRECT_URI,
        )
    else:
        print("[INFO] EXPECTED_GOOGLE_REDIRECT_URI not set; skipping exact redirect match.")

    failures += ok(
        "FRONTEND_URL origins valid",
        all(valid_abs_url(o) for o in s.frontend_origins),
        all(valid_abs_url(o) for o in s.frontend_origins),
    )
    if EXPECTED_FRONTEND_ORIGINS:
        failures += ok(
            "FRONTEND_URL contains expected origins",
            set(s.frontend_origins) == EXPECTED_FRONTEND_ORIGINS,
            set(s.frontend_origins) == EXPECTED_FRONTEND_ORIGINS,
        )
    else:
        print("[INFO] EXPECTED_FRONTEND_URLS not set; skipping exact frontend origin match.")

    failures += ok("AUTH_POST_LOGIN_REDIRECT valid", valid_abs_url(s.post_login_redirect), valid_abs_url(s.post_login_redirect))
    failures += ok(
        "AUTH_POST_LOGIN_REDIRECT allowed",
        s.post_login_redirect.rstrip("/") in {o.rstrip("/") for o in s.frontend_origins},
        s.post_login_redirect.rstrip("/") in {o.rstrip("/") for o in s.frontend_origins},
    )
    if EXPECTED_POST_LOGIN:
        failures += ok(
            "AUTH_POST_LOGIN_REDIRECT matches expected",
            s.post_login_redirect == EXPECTED_POST_LOGIN,
            s.post_login_redirect == EXPECTED_POST_LOGIN,
        )
    else:
        print("[INFO] EXPECTED_AUTH_POST_LOGIN_REDIRECT not set; skipping exact post-login match.")

    failures += ok(
        "GOOGLE_AUTHORIZATION_ENDPOINT valid",
        valid_abs_url(s.google_authorization_endpoint),
        valid_abs_url(s.google_authorization_endpoint),
    )
    failures += ok(
        "GOOGLE_AUTHORIZATION_ENDPOINT matches expected",
        s.google_authorization_endpoint == EXPECTED_AUTH_ENDPOINT,
        s.google_authorization_endpoint == EXPECTED_AUTH_ENDPOINT,
    )
    failures += ok(
        "GOOGLE_TOKEN_ENDPOINT valid",
        valid_abs_url(s.google_token_endpoint),
        valid_abs_url(s.google_token_endpoint),
    )
    failures += ok(
        "GOOGLE_TOKEN_ENDPOINT matches expected",
        s.google_token_endpoint == EXPECTED_TOKEN_ENDPOINT,
        s.google_token_endpoint == EXPECTED_TOKEN_ENDPOINT,
    )

    failures += ok(
        "AUTH_COOKIE_SECURE is set",
        isinstance(s.cookie_secure, bool),
        isinstance(s.cookie_secure, bool),
    )
    failures += ok(
        "AUTH_COOKIE_SAME_SITE is lax/strict/none",
        s.cookie_same_site in {"lax", "strict", "none"},
        s.cookie_same_site in {"lax", "strict", "none"},
    )
    if s.cookie_same_site == "none":
        failures += ok(
            "AUTH_COOKIE_SAME_SITE=none requires AUTH_COOKIE_SECURE=true",
            bool(s.cookie_secure),
            bool(s.cookie_secure),
        )

    failures += ok(
        "Callback route /auth/google/callback registered",
        route_exists("/auth/google/callback", "GET"),
        route_exists("/auth/google/callback", "GET"),
    )
    failures += ok(
        "Login route /auth/google/login registered",
        route_exists("/auth/google/login", "GET"),
        route_exists("/auth/google/login", "GET"),
    )

    docker_redirect = os.getenv("DOCKER_GOOGLE_REDIRECT_URI")
    docker_frontend = os.getenv("DOCKER_FRONTEND_URL")
    if docker_redirect or docker_frontend:
        if EXPECTED_REDIRECT_URI:
            failures += ok(
                "DOCKER_GOOGLE_REDIRECT_URI matches",
                docker_redirect == EXPECTED_REDIRECT_URI,
                docker_redirect == EXPECTED_REDIRECT_URI,
            )
        if EXPECTED_FRONTEND_ORIGINS:
            failures += ok(
                "DOCKER_FRONTEND_URL contains expected",
                set(filter(None, (docker_frontend or "").split(","))) == EXPECTED_FRONTEND_ORIGINS,
                set(filter(None, (docker_frontend or "").split(","))) == EXPECTED_FRONTEND_ORIGINS,
            )
    else:
        print("[INFO] Docker runtime env not provided (set DOCKER_GOOGLE_REDIRECT_URI/DOCKER_FRONTEND_URL to compare).")

    print(f"SUMMARY: {'PASS' if failures == 0 else 'FAIL'} ({failures} issue(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
