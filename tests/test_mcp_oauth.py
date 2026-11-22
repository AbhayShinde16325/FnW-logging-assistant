from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth.config as auth_config
import auth.mcp_oauth_routes as mcp_oauth_routes
import auth.routes as auth_routes
import auth.service as auth_service
from auth.mcp_oauth import MCPOAuthSession, _pkce_matches
from auth.permissions import ROLE_EMPLOYEE
from auth.service import create_session, find_or_link_google_employee, sync_rbac
from database.connection import Base
from database.models import Employee, MCPOAuthCode, MCPOAuthSession as MCPOAuthSessionModel, Role
from mcp_server.security.execution_context import MCPAuthenticationError, execution_context_from_token
from auth.mcp_oauth import canonical_mcp_resource


def _settings(**overrides):
    base = auth_config.load_auth_settings()
    values = {**base.__dict__}
    values.update({
        "required": True,
        "secret_key": "x" * 48,
        "google_client_id": "gid",
        "google_client_secret": "gsecret",
        "google_redirect_uri": "http://localhost:8000/auth/google/callback",
        "frontend_origins": ("http://localhost:5173",),
        "post_login_redirect": "http://localhost:5173",
    })
    values.update(overrides)
    return type(base)(**values)


@pytest.fixture
def oauth_env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    import auth.mcp_oauth as mcp_oauth
    monkeypatch.setattr(mcp_oauth, "SessionLocal", factory)
    settings = _settings()
    monkeypatch.setattr(auth_config, "settings", settings)
    monkeypatch.setattr(auth_routes, "auth_config", auth_config)
    monkeypatch.setattr(mcp_oauth_routes, "auth_config", auth_config)
    sync_rbac()
    session = factory()
    role = session.query(Role).filter(Role.name == ROLE_EMPLOYEE).one()
    emp = Employee(name="Rahul", email="rahul@example.com", role_id=role.id, is_active=True)
    session.add(emp)
    session.commit()
    session.refresh(emp)
    session.close()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(mcp_oauth_routes.router)
    client = TestClient(app, follow_redirects=False)
    return {"client": client, "settings": settings, "factory": factory, "employee_id": emp.id}


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(client):
    r = client.post("/oauth/register", json={
        "client_name": "ChatGPT",
        "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp:tools",
    })
    assert r.status_code == 201
    return r.json()["client_id"]


def test_oauth_metadata_and_authorize_redirect(oauth_env, monkeypatch):
    client = oauth_env["client"]
    verifier, challenge = _pkce()
    # avoid real Google
    monkeypatch.setattr("auth.google.authorization_url", lambda **kw: "https://accounts.google.com/o/oauth2/v2/auth?state=" + kw["state"])

    meta = client.get("/.well-known/oauth-authorization-server")
    assert meta.status_code == 200
    body = meta.json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["authorization_response_iss_parameter_supported"] is True

    resource = canonical_mcp_resource(oauth_env["settings"])
    client_id = _register(client)
    r = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
        "state": "abc123",
        "scope": "mcp:tools",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    })
    assert r.status_code == 302
    assert "accounts.google.com" in r.headers["location"]


def test_full_oauth_code_pkce_refresh_and_mcp_token(oauth_env, monkeypatch):
    client = oauth_env["client"]
    settings = oauth_env["settings"]
    verifier, challenge = _pkce()

    monkeypatch.setattr("auth.google.authorization_url", lambda **kw: "https://accounts.google.com/o/oauth2/v2/auth?state=" + kw["state"])
    monkeypatch.setattr(auth_routes, "exchange_code", lambda **kw: {"id_token": "id"})
    monkeypatch.setattr(auth_routes, "verify_id_token", lambda **kw: {"sub": "sub1", "email": "rahul@example.com", "name": "Rahul"})

    resource = canonical_mcp_resource(settings)
    client_id = _register(client)
    auth = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
        "state": "client-state",
        "scope": "mcp:tools",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    })
    assert auth.status_code == 302
    # Google state from redirect
    gs = parse_qs(urlparse(auth.headers["location"]).query)["state"][0]
    cb = client.get("/auth/google/callback", params={"code": "gc", "state": gs})
    assert cb.status_code == 302
    loc = cb.headers["location"]
    assert loc.startswith("https://chatgpt.com/connector/oauth/callback")
    q = parse_qs(urlparse(loc).query)
    code = q["code"][0]
    assert q["state"][0] == "client-state"

    assert "iss=" in loc
    token = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": resource,
    })
    assert token.status_code == 200
    payload = token.json()
    assert payload["token_type"] == "Bearer"
    assert "refresh_token" in payload
    ctx = execution_context_from_token(payload["access_token"])
    assert ctx.employee.email == "rahul@example.com"
    assert ctx.actor.id == oauth_env["employee_id"]

    refreshed = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": payload["refresh_token"],
        "client_id": client_id,
        "resource": resource,
    })
    assert refreshed.status_code == 200
    payload2 = refreshed.json()
    assert payload2["access_token"] != payload["access_token"]

    revoked = client.post("/oauth/revoke", data={"token": payload2["refresh_token"]})
    assert revoked.status_code == 200
    denied = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": payload2["refresh_token"],
        "client_id": client_id,
        "resource": resource,
    })
    assert denied.status_code == 400


def test_oauth_rejects_unknown_or_inactive_employee(oauth_env, monkeypatch):
    client = oauth_env["client"]
    verifier, challenge = _pkce()
    monkeypatch.setattr("auth.google.authorization_url", lambda **kw: "https://accounts.google.com/o/oauth2/v2/auth?state=" + kw["state"])
    monkeypatch.setattr(auth_routes, "exchange_code", lambda **kw: {"id_token": "id"})
    monkeypatch.setattr(auth_routes, "verify_id_token", lambda **kw: {"sub": "sub2", "email": "unknown@example.com", "name": "Nope"})

    resource = canonical_mcp_resource(oauth_env["settings"])
    client_id = _register(client)
    auth = client.get("/oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
        "state": "s",
        "scope": "mcp:tools",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    })
    gs = parse_qs(urlparse(auth.headers["location"]).query)["state"][0]
    cb = client.get("/auth/google/callback", params={"code": "gc", "state": gs})
    assert cb.status_code == 302
    assert "auth_error=not_provisioned" in cb.headers["location"]
