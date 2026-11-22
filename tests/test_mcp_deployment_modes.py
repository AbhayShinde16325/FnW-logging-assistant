from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_server.configuration as mcp_config
import mcp_server.server as mcp_server


def _reload_settings(monkeypatch, **env):
    keys = [
        "APP_ENV",
        "MCP_ENABLED",
        "MCP_TRANSPORT",
        "MCP_SERVER_NAME",
        "MCP_PATH",
        "MCP_REQUIRE_AUTH",
        "MCP_SERVER_URL",
        "MCP_PUBLIC_RESOURCE_URL",
        "MCP_TUNNEL_ENABLED",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    mcp_config.clear_mcp_settings_cache()
    mcp_server.settings = mcp_config.load_mcp_settings()
    mcp_server.mcp_asgi_app.settings = mcp_server.settings
    return mcp_server.settings


def test_development_mode_defaults(monkeypatch):
    settings = _reload_settings(
        monkeypatch,
        APP_ENV="development",
        MCP_ENABLED="true",
        MCP_REQUIRE_AUTH="true",
    )
    assert settings.app_env == "development"
    assert settings.is_production is False
    assert settings.tunnel_enabled is True
    assert settings.require_auth is True
    assert settings.server_url.startswith("http://127.0.0.1")


def test_production_mode_disables_tunnel_by_default(monkeypatch):
    settings = _reload_settings(
        monkeypatch,
        APP_ENV="production",
        MCP_ENABLED="true",
        MCP_REQUIRE_AUTH="true",
        MCP_SERVER_URL="http://127.0.0.1:8000/mcp",
        MCP_PUBLIC_RESOURCE_URL="https://fnw-logging-assistant.onrender.com",
    )
    assert settings.app_env == "production"
    assert settings.is_production is True
    assert settings.tunnel_enabled is False
    assert settings.require_auth is True
    assert settings.server_url == "http://127.0.0.1:8000/mcp"
    assert (
        settings.public_resource_url
        == "https://fnw-logging-assistant.onrender.com/mcp"
    )


def test_public_resource_url_canonicalizes_origin(monkeypatch):
    settings = _reload_settings(
        monkeypatch,
        APP_ENV="production",
        MCP_ENABLED="true",
        MCP_PATH="/mcp",
        MCP_PUBLIC_RESOURCE_URL="https://fnw-logging-assistant.onrender.com/",
    )
    assert (
        settings.public_resource_url
        == "https://fnw-logging-assistant.onrender.com/mcp"
    )


def test_protected_resource_metadata_uses_public_url(monkeypatch):
    _reload_settings(
        monkeypatch,
        APP_ENV="production",
        MCP_ENABLED="true",
        MCP_PATH="/mcp",
        MCP_REQUIRE_AUTH="true",
        MCP_PUBLIC_RESOURCE_URL="https://fnw-logging-assistant.onrender.com",
    )
    app = FastAPI()
    mcp_server.mount_mcp(app)
    client = TestClient(app)

    response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://fnw-logging-assistant.onrender.com/mcp"
    assert body["authorization_servers"] == ["https://fnw-logging-assistant.onrender.com"]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == ["mcp:tools"]


def test_mcp_endpoint_requires_auth_even_in_production_mode(monkeypatch):
    _reload_settings(
        monkeypatch,
        APP_ENV="production",
        MCP_ENABLED="true",
        MCP_REQUIRE_AUTH="true",
        MCP_PUBLIC_RESOURCE_URL="https://fnw-logging-assistant.onrender.com",
    )
    app = FastAPI()
    mcp_server.mount_mcp(app)
    client = TestClient(app)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 401
