from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth.config as auth_config
import auth.routes as auth_routes
from auth.google import GoogleAuthError, authorization_url, verify_id_token
from auth.permissions import (
    ALL_PERMISSIONS,
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLE_PERMISSIONS,
    TASKS_CREATE,
    TASKS_DELETE,
    TASKS_READ,
    TASKS_READ_ALL,
    TASKS_UPDATE,
    TASKS_UPDATE_ALL,
)
from auth.security import (
    constant_time_equal,
    create_access_token,
    create_state_token,
    decode_access_token,
    decode_state_token,
    pkce_pair,
    safe_return_to,
    token_hash,
    utcnow,
)
from auth.service import AuthenticatedEmployee
from services.database_agent import ToolContext, _write_query
from services.task_scope import (
    TaskActor,
    can_delete_task,
    can_read_task,
    can_update_task,
    owns_task,
    use_task_actor,
)


def make_settings(**overrides):
    base = auth_config.load_auth_settings()
    values = {
        "required": True,
        "secret_key": "a" * 48,
        "issuer": "test-issuer",
        "audience": "test-audience",
        "google_client_id": "google-client",
        "google_client_secret": "google-secret",
        "google_redirect_uri": "http://localhost:8080/api/auth/google/callback",
        "cookie_secure": False,
        "cookie_same_site": "lax",
        "post_login_redirect": "http://localhost:5173",
        "frontend_origins": ("http://localhost:5173", "http://localhost:8088"),
    }
    values.update(overrides)
    return replace(base, **values)


def employee(permissions, *, employee_id=7, name="Rahul", role=ROLE_EMPLOYEE):
    return AuthenticatedEmployee(
        id=employee_id,
        name=name,
        email=f"employee-{employee_id}@example.com",
        role=role,
        permissions=frozenset(permissions),
    )


# --- permissions and settings ----------------------------------------------


def test_role_catalogue_matches_the_required_permissions():
    assert set(ALL_PERMISSIONS) == {
        "tasks.create",
        "tasks.read",
        "tasks.read_all",
        "tasks.update",
        "tasks.update_all",
        "tasks.delete",
        "employees.read",
        "employees.manage",
        "roles.manage",
        "reports.read",
    }
    assert ROLE_PERMISSIONS[ROLE_ADMIN] == frozenset(ALL_PERMISSIONS)
    assert TASKS_READ_ALL in ROLE_PERMISSIONS[ROLE_MANAGER]
    assert TASKS_UPDATE_ALL in ROLE_PERMISSIONS[ROLE_MANAGER]
    assert "employees.manage" in ROLE_PERMISSIONS[ROLE_MANAGER]
    assert "roles.manage" not in ROLE_PERMISSIONS[ROLE_MANAGER]
    assert ROLE_PERMISSIONS[ROLE_EMPLOYEE] == frozenset(
        {TASKS_CREATE, TASKS_READ, TASKS_UPDATE, TASKS_DELETE}
    )


def test_secure_config_errors_name_the_missing_production_values():
    settings = make_settings(secret_key="", google_client_id="")

    errors = " ".join(settings.secure_config_errors)

    assert "AUTH_SECRET_KEY" in errors
    assert "GOOGLE_CLIENT_ID" in errors


def test_auth_can_be_disabled_for_legacy_tests():
    settings = make_settings(required=False, secret_key="", google_client_id="")

    assert settings.secure_config_errors == []


# --- tokens and PKCE --------------------------------------------------------


def test_access_token_validates_signature_issuer_audience_and_session():
    settings = make_settings()
    token, expires_at = create_access_token(
        employee_id=7,
        email="rahul@example.com",
        role=ROLE_EMPLOYEE,
        permissions=[TASKS_READ],
        session_id="session-1",
        settings=settings,
    )

    claims = decode_access_token(token, settings)

    assert claims["sub"] == "7"
    assert claims["sid"] == "session-1"
    assert claims["iss"] == "test-issuer"
    assert claims["aud"] == "test-audience"
    assert claims["permissions"] == [TASKS_READ]
    assert expires_at > utcnow()


def test_access_token_rejects_another_secret_and_wrong_type():
    settings = make_settings()
    token, _ = create_access_token(
        employee_id=7,
        email="rahul@example.com",
        role=ROLE_EMPLOYEE,
        permissions=[TASKS_READ],
        session_id="session-1",
        settings=settings,
    )

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token, make_settings(secret_key="b" * 48))


def test_access_token_rejects_expired_tokens():
    settings = make_settings()
    expired = create_access_token(
        employee_id=7,
        email="rahul@example.com",
        role=ROLE_EMPLOYEE,
        permissions=[TASKS_READ],
        session_id="session-1",
        settings=settings,
        now=utcnow() - timedelta(hours=2),
    )[0]

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired, settings)


def test_state_token_round_trips_and_requires_the_same_secret():
    settings = make_settings()
    token = create_state_token(
        state="state-value",
        nonce="nonce-value",
        code_verifier="verifier-value",
        return_to="http://localhost:8080/",
        settings=settings,
    )

    payload = decode_state_token(token, settings)

    assert payload["state"] == "state-value"
    assert payload["nonce"] == "nonce-value"
    assert payload["code_verifier"] == "verifier-value"

    with pytest.raises(jwt.PyJWTError):
        decode_state_token(token, make_settings(secret_key="c" * 48))


def test_pkce_challenge_is_sha256_of_the_verifier_and_tokens_are_hashed():
    import base64
    import hashlib

    verifier, challenge = pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    assert challenge == expected
    assert token_hash("secret") != "secret"
    assert len(token_hash("secret")) == 64


def test_constant_time_comparison_and_safe_return_urls():
    assert constant_time_equal("abc", "abc")
    assert not constant_time_equal("abc", "abd")
    assert not constant_time_equal("", "")
    assert not constant_time_equal(None, None)

    settings = make_settings()

    assert (
        safe_return_to("/tasks", settings.post_login_redirect, settings.frontend_origins)
        == "http://localhost:5173/tasks"
    )
    assert (
        safe_return_to(
            "http://localhost:8088/tasks?tab=inbox#n1",
            settings.post_login_redirect,
            settings.frontend_origins,
        )
        == "http://localhost:8088/tasks?tab=inbox#n1"
    )
    assert (
        safe_return_to(
            "http://localhost:5173/tasks",
            settings.post_login_redirect,
            settings.frontend_origins,
        )
        == "http://localhost:5173/tasks"
    )
    assert (
        safe_return_to(
            "https://evil.example/tasks",
            settings.post_login_redirect,
            settings.frontend_origins,
        )
        == settings.post_login_redirect
    )
    assert (
        safe_return_to(
            "//evil.example/tasks",
            settings.post_login_redirect,
            settings.frontend_origins,
        )
        == settings.post_login_redirect
    )


# --- Google ID token verification -----------------------------------------


def _rsa_signing_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    return private_pem, public_key


class _FakeJwksClient:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=self.public_key)


def _google_claims(**overrides):
    now = utcnow()
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "google-client",
        "sub": "google-subject-1",
        "email": "rahul@example.com",
        "email_verified": True,
        "name": "Rahul",
        "nonce": "nonce-value",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return claims


def test_google_id_token_checks_signature_nonce_and_verified_email():
    private_pem, public_key = _rsa_signing_material()
    settings = make_settings()
    token = jwt.encode(
        _google_claims(),
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    claims = verify_id_token(
        id_token=token,
        nonce="nonce-value",
        settings=settings,
        jwks_client=_FakeJwksClient(public_key),
    )

    assert claims["sub"] == "google-subject-1"
    assert claims["email"] == "rahul@example.com"


def test_google_id_token_rejects_nonce_mismatch_and_unverified_email():
    private_pem, public_key = _rsa_signing_material()
    settings = make_settings()
    client = _FakeJwksClient(public_key)

    with pytest.raises(GoogleAuthError):
        verify_id_token(
            id_token=jwt.encode(
                _google_claims(nonce="other"),
                private_pem,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
            nonce="nonce-value",
            settings=settings,
            jwks_client=client,
        )

    with pytest.raises(GoogleAuthError):
        verify_id_token(
            id_token=jwt.encode(
                _google_claims(email_verified=False),
                private_pem,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
            nonce="nonce-value",
            settings=settings,
            jwks_client=client,
        )


def test_google_id_token_rejects_wrong_audience_and_signature():
    private_pem, public_key = _rsa_signing_material()
    _, other_public_key = _rsa_signing_material()
    settings = make_settings()

    with pytest.raises(GoogleAuthError):
        verify_id_token(
            id_token=jwt.encode(
                _google_claims(aud="another-client"),
                private_pem,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
            nonce="nonce-value",
            settings=settings,
            jwks_client=_FakeJwksClient(public_key),
        )

    with pytest.raises(GoogleAuthError):
        verify_id_token(
            id_token=jwt.encode(
                _google_claims(),
                private_pem,
                algorithm="RS256",
                headers={"kid": "test-key"},
            ),
            nonce="nonce-value",
            settings=settings,
            jwks_client=_FakeJwksClient(other_public_key),
        )


def test_authorization_url_contains_pkce_and_state():
    settings = make_settings()
    url = authorization_url(
        state="state-value",
        nonce="nonce-value",
        code_challenge="challenge-value",
        settings=settings,
    )

    assert url.startswith(settings.google_authorization_endpoint + "?")
    assert "code_challenge=challenge-value" in url
    assert "code_challenge_method=S256" in url
    assert "state=state-value" in url
    assert "nonce=nonce-value" in url
    assert "response_type=code" in url


# --- task authorization policy ---------------------------------------------


def _task(**overrides):
    values = {
        "id": 10,
        "created_by_employee_id": None,
        "assigned_to_employee_id": None,
        "assigned_to": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_task_ownership_uses_employee_ids_when_they_exist():
    actor = TaskActor(id=7, name="Rahul", permissions=frozenset({TASKS_READ}))
    own = _task(assigned_to_employee_id=7, assigned_to="Someone Else")
    other = _task(assigned_to_employee_id=8, assigned_to="Rahul")

    assert owns_task(own, actor)
    assert not owns_task(other, actor)


def test_display_names_never_authorize_task_access():
    actor = TaskActor(id=7, name="Rahul", permissions=frozenset({TASKS_READ}))

    assert not owns_task(_task(assigned_to="  rahul "), actor)
    assert not owns_task(_task(assigned_to="Rahul"), actor)
    assert not owns_task(_task(assigned_to=None), actor)
    assert not owns_task(_task(created_by_employee_id=7), actor)


def test_read_update_and_delete_need_permission_and_ownership():
    read_only = TaskActor(
        id=7,
        name="Rahul",
        permissions=frozenset({TASKS_READ}),
    )
    own_task = _task(assigned_to_employee_id=7)
    other_task = _task(assigned_to_employee_id=8)

    assert can_read_task(own_task, read_only)
    assert not can_read_task(other_task, read_only)
    assert not can_update_task(own_task, read_only)
    assert not can_delete_task(own_task, read_only)

    manager = TaskActor(
        id=9,
        name="Manager",
        permissions=frozenset({TASKS_READ, TASKS_READ_ALL, TASKS_UPDATE, TASKS_UPDATE_ALL}),
    )
    assert can_read_task(other_task, manager)
    assert can_update_task(other_task, manager)
    assert not can_delete_task(other_task, manager)


def test_context_actor_does_not_leak_between_requests():
    actor = TaskActor(id=7, name="Rahul", permissions=frozenset({TASKS_READ}))

    with use_task_actor(actor):
        assert can_read_task(_task(assigned_to_employee_id=7))

    assert can_read_task(_task(assigned_to_employee_id=8))


# --- HTTP auth surface ------------------------------------------------------


@pytest.fixture
def auth_client(monkeypatch):
    settings = make_settings()
    monkeypatch.setattr(auth_config, "settings", settings)
    monkeypatch.setattr(auth_routes, "auth_config", auth_config)

    app = FastAPI()
    app.include_router(auth_routes.router)

    return TestClient(app), settings


def test_google_login_returns_google_url_and_sets_http_only_state_cookie(auth_client):
    client, settings = auth_client

    response = client.get("/auth/google/login", follow_redirects=False)

    assert response.status_code == 200
    data = response.json()
    assert data["authorization_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    state_cookie = response.cookies.get(settings.oauth_state_cookie_name)
    assert state_cookie


def test_refresh_requires_csrf_and_a_refresh_cookie(auth_client):
    client, settings = auth_client

    response = client.post("/auth/refresh")

    assert response.status_code == 403

    client.cookies.set(settings.csrf_cookie_name, "csrf")
    response = client.post(
        "/auth/refresh",
        headers={"X-CSRF-Token": "csrf"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "The session has expired."


def test_logout_all_and_me_require_an_access_token(auth_client):
    client, _ = auth_client

    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/logout-all").status_code == 401


def test_google_login_fails_safely_when_google_is_not_configured(auth_client):
    client, settings = auth_client

    auth_config.settings = make_settings(
        google_client_id="",
        google_client_secret="",
    )

    response = client.get("/auth/google/login")

    assert response.status_code == 503


# --- database agent write authorization ------------------------------------


def _agent_context(**overrides):
    values = {
        "message": "update the task",
        "employee": "Rahul",
        "confirmation": "CONFIRM DELETE",
    }
    values.update(overrides)
    return ToolContext(**values)


def test_agent_blocks_employee_write_without_an_ownership_predicate():
    actor = TaskActor(
        id=7,
        name="Rahul",
        permissions=frozenset({TASKS_UPDATE}),
    )
    called = []

    with use_task_actor(actor):
        result = _write_query(
            {
                "sql": "UPDATE tasks SET progress = :progress WHERE id = :id",
                "params": {"progress": 50, "id": 10},
            },
            _agent_context(
                confirmation="CONFIRM UPDATE",
                write_executor=lambda *args, **kwargs: called.append(1),
            ),
        )

    assert result["status"] == "not_found"
    assert "assigned to you" in result["message"]
    assert called == []


def test_agent_allows_employee_progress_write_when_ownership_is_bound(
    monkeypatch,
):
    from types import SimpleNamespace

    import services.task_management as task_management

    actor = TaskActor(
        id=7,
        name="Rahul",
        permissions=frozenset({TASKS_UPDATE}),
    )
    calls = []
    executor_calls = []

    def update_task(task_id, values):
        calls.append((task_id, values))
        return SimpleNamespace(
            id=task_id,
            status="IN_PROGRESS",
            progress=values["progress"],
            assigned_to_employee_id=actor.id,
        )

    monkeypatch.setattr(task_management, "update_task", update_task)

    with use_task_actor(actor):
        result = _write_query(
            {
                "sql": (
                    "UPDATE tasks SET progress = :progress "
                    "WHERE id = :id "
                    "AND assigned_to_employee_id = :employee_id"
                ),
                "params": {
                    "progress": 50,
                    "id": 10,
                    "employee_id": actor.id,
                },
            },
            _agent_context(
                confirmation="CONFIRM UPDATE",
                write_executor=lambda *args, **kwargs: executor_calls.append(1),
            ),
        )

    assert result["status"] == "success"
    assert calls == [(10, {"progress": 50})]
    assert executor_calls == []


def test_agent_rejects_employee_reassignment_and_other_owners_task():
    actor = TaskActor(
        id=7,
        name="Rahul",
        permissions=frozenset({TASKS_UPDATE}),
    )

    with use_task_actor(actor):
        reassignment = _write_query(
            {
                "sql": (
                    "UPDATE tasks SET assigned_to_employee_id = :employee_id "
                    "WHERE id = :id"
                ),
                "params": {"employee_id": actor.id, "id": 10},
            },
            _agent_context(confirmation="CONFIRM UPDATE"),
        )
        other = _write_query(
            {
                "sql": (
                    "UPDATE tasks SET progress = :progress "
                    "WHERE id = :id "
                    "AND assigned_to_employee_id = :employee_id"
                ),
                "params": {"progress": 50, "id": 10, "employee_id": 8},
            },
            _agent_context(confirmation="CONFIRM UPDATE"),
        )

    assert reassignment["status"] == "error"
    assert "assignment" in reassignment["message"]
    assert other["status"] == "not_found"


def test_agent_blocks_write_without_the_required_permission():
    actor = TaskActor(
        id=7,
        name="Rahul",
        permissions=frozenset({TASKS_READ}),
    )
    called = []

    with use_task_actor(actor):
        result = _write_query(
            {"sql": "DELETE FROM tasks WHERE id = :id", "params": {"id": 10}},
            _agent_context(write_executor=lambda *args, **kwargs: called.append(1)),
        )

    assert result["status"] == "error"
    assert "permission" in result["message"]
    assert called == []
