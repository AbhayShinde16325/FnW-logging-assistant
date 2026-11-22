"""Tests for dedicated MCP client credentials."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from auth.security import utcnow
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import auth.config as auth_config
import auth.employee_service as employee_service
import auth.mcp_credentials as mcp_credentials
import auth.service as auth_service
import database.task_repository as task_repository
import services.conversation_memory as conversation_memory
import services.task_management as task_management
from auth.employee_routes import router as employees_router
from auth.mcp_credential_routes import router as mcp_credentials_router
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_MANAGER
from auth.security import create_access_token
from auth.service import bootstrap_admin, create_session, sync_rbac
from database.connection import Base
from database.models import Employee, MCPClientCredential, Role, Task
from mcp_server.security.execution_context import (
    MCPAuthenticationError,
    execution_context_from_token,
)


@pytest.fixture
def mcp_cred_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for module in (
        auth_service,
        employee_service,
        conversation_memory,
        task_management,
        task_repository,
        mcp_credentials,
    ):
        monkeypatch.setattr(module, "SessionLocal", factory)

    class _ReminderValidationError(Exception):
        pass

    notifications = SimpleNamespace(
        ReminderValidationError=_ReminderValidationError,
        TYPE_TASK_ASSIGNED="task_assigned",
        PRIORITY_HIGH="HIGH",
        utcnow=lambda: datetime.now(timezone.utc),
        validate_reminder_values=lambda **_: None,
        create_task_event_notification=lambda **_: None,
        cancel_pending_notifications=lambda **_: None,
    )
    monkeypatch.setattr(task_management, "notification_service", notifications)

    settings = replace(
        auth_config.load_auth_settings(),
        required=True,
        secret_key="mcp-credential-test-secret-key-1234567890",
        google_client_id="google-client",
        google_client_secret="google-secret",
        access_token_minutes=30,
        refresh_token_days=14,
    )
    monkeypatch.setattr(auth_config, "settings", settings)
    sync_rbac()

    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    manager_id = _add_employee(factory, "manager@example.com", "Manager", ROLE_MANAGER)
    employee_id = _add_employee(factory, "employee@example.com", "Rahul", ROLE_EMPLOYEE)
    other_id = _add_employee(factory, "other@example.com", "Neha", ROLE_EMPLOYEE)
    own_task = _add_task(factory, "Own task", "Rahul", employee_id, manager_id, 10)
    other_task = _add_task(factory, "Other task", "Neha", other_id, manager_id, 20)

    app = FastAPI()
    app.include_router(mcp_credentials_router)
    app.include_router(employees_router)

    return {
        "factory": factory,
        "settings": settings,
        "admin": admin,
        "manager_id": manager_id,
        "employee_id": employee_id,
        "other_id": other_id,
        "own_task": own_task,
        "other_task": other_task,
        "client": TestClient(app),
    }


def _add_employee(factory, email, name, role_name):
    session = factory()
    try:
        role = session.query(Role).filter(Role.name == role_name).one()
        employee = Employee(email=email, name=name, role_id=role.id, is_active=True)
        session.add(employee)
        session.commit()
        return employee.id
    finally:
        session.close()


def _add_task(factory, title, assigned_to, assigned_to_employee_id, created_by_employee_id, progress):
    session = factory()
    try:
        task = Task(
            task_title=title,
            task_description="Desc",
            assigned_to=assigned_to,
            assigned_to_employee_id=assigned_to_employee_id,
            created_by_employee_id=created_by_employee_id,
            status="PENDING",
            progress=progress,
        )
        session.add(task)
        session.commit()
        return task.id
    finally:
        session.close()


def _access_token(employee_id, settings):
    token, session_row = create_session(employee_id, auth_settings=settings)
    _ = token
    session = session_row
    # Need full identity/permissions; use employee_for_session through route expectations by minting JWT directly.
    from auth.service import employee_for_session

    identity = employee_for_session(session.id, session.employee_id)
    assert identity is not None
    jwt_token, _ = create_access_token(
        employee_id=identity.id,
        email=identity.email,
        role=identity.role,
        permissions=sorted(identity.permissions),
        session_id=session.id,
        settings=settings,
    )
    return jwt_token


def _auth_headers(employee_id, settings):
    return {"Authorization": f"Bearer {_access_token(employee_id, settings)}"}


def test_generate_credential_has_prefix_and_entropy():
    secret = mcp_credentials.generate_credential_secret()
    assert secret.startswith("fnw_mcp_")
    # base64url encoding of 32 bytes is 43 chars without padding
    assert len(secret) >= len("fnw_mcp_") + 32


def test_create_credential_returns_plaintext_once_and_stores_hash_only(mcp_cred_env):
    env = mcp_cred_env
    plaintext, record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"],
        name="OpenClaw",
        created_by_employee_id=env["employee_id"],
    )

    assert plaintext.startswith("fnw_mcp_")
    assert record.token_prefix == plaintext[:20]
    assert record.employee_id == env["employee_id"]
    assert record.revoked_at is None

    session = env["factory"]()
    try:
        row = session.query(MCPClientCredential).filter(MCPClientCredential.id == record.id).one()
        assert row.token_hash == mcp_credentials.hash_credential(plaintext)
        assert row.token_hash != plaintext
        assert plaintext not in (row.token_prefix or "")
        assert getattr(row, "plaintext", None) is None
    finally:
        session.close()

    records = mcp_credentials.list_mcp_credentials(employee_id=env["employee_id"])
    assert len(records) == 1
    payload = records[0].to_payload()
    assert "credential" not in payload
    assert "token_hash" not in payload
    assert payload["token_prefix"] == record.token_prefix


def test_create_credential_rejects_inactive_employee_and_past_expiry(mcp_cred_env):
    env = mcp_cred_env
    session = env["factory"]()
    try:
        employee = session.query(Employee).filter(Employee.id == env["other_id"]).one()
        employee.is_active = False
        session.commit()
    finally:
        session.close()

    with pytest.raises(mcp_credentials.MCPCredentialForbidden):
        mcp_credentials.create_mcp_credential(
            employee_id=env["other_id"], name="Nope", created_by_employee_id=env["other_id"]
        )

    with pytest.raises(mcp_credentials.MCPCredentialError):
        mcp_credentials.create_mcp_credential(
            employee_id=env["employee_id"],
            name="Expired",
            created_by_employee_id=env["employee_id"],
            expires_at=utcnow() - timedelta(minutes=1),
        )


def test_valid_mcp_credential_resolves_active_employee_and_updates_last_used(mcp_cred_env):
    env = mcp_cred_env
    plaintext, record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="Agent", created_by_employee_id=env["employee_id"]
    )

    context = execution_context_from_token(plaintext)
    assert context.employee.id == env["employee_id"]
    assert context.actor.id == env["employee_id"]
    assert context.access_token == plaintext

    session = env["factory"]()
    try:
        row = session.query(MCPClientCredential).filter(MCPClientCredential.id == record.id).one()
        assert row.last_used_at is not None
    finally:
        session.close()


def test_unknown_expired_revoked_and_deactivated_credentials_rejected(mcp_cred_env):
    env = mcp_cred_env

    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("fnw_mcp_" + "x" * 40)

    expired_plain, expired_record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"],
        name="Soon expired",
        created_by_employee_id=env["employee_id"],
        expires_at=utcnow() + timedelta(minutes=5),
    )
    session = env["factory"]()
    try:
        row = session.query(MCPClientCredential).filter(MCPClientCredential.id == expired_record.id).one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    finally:
        session.close()
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(expired_plain)

    revoked_plain, revoked_record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="Revoked", created_by_employee_id=env["employee_id"]
    )
    mcp_credentials.revoke_mcp_credential(
        credential_id=revoked_record.id, actor_employee_id=env["employee_id"]
    )
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(revoked_plain)

    deactivated_plain, _ = mcp_credentials.create_mcp_credential(
        employee_id=env["other_id"], name="Will deactivate", created_by_employee_id=env["other_id"]
    )
    session = env["factory"]()
    try:
        employee = session.query(Employee).filter(Employee.id == env["other_id"]).one()
        employee.is_active = False
        session.commit()
    finally:
        session.close()
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(deactivated_plain)


def test_employee_access_jwt_is_rejected_by_mcp(mcp_cred_env):
    env = mcp_cred_env
    token = _access_token(env["employee_id"], env["settings"])
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(token)


def test_refresh_token_is_rejected_by_mcp(mcp_cred_env):
    env = mcp_cred_env
    refresh_token, _session = create_session(
        env["employee_id"], auth_settings=env["settings"]
    )
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(refresh_token)


def test_invalid_and_malformed_mcp_credentials_rejected(mcp_cred_env):
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("fnw_mcp_not-a-real-credential")
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("Bearer fnw_mcp_nope")
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("not-even-close")


def test_mcp_credential_works_after_browser_logout(mcp_cred_env):
    """Logout revokes refresh/session state only; MCP credentials are independent."""
    env = mcp_cred_env
    refresh_token, session_row = create_session(
        env["employee_id"], auth_settings=env["settings"]
    )
    assert auth_service.revoke_refresh_session(refresh_token) is True
    assert (
        auth_service.employee_for_session(session_row.id, env["employee_id"])
        is None
    )

    plaintext, _ = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"],
        name="Post logout",
        created_by_employee_id=env["employee_id"],
    )
    context = execution_context_from_token(plaintext)
    assert context.employee.id == env["employee_id"]
    assert context.actor.id == env["employee_id"]


def test_mcp_credential_works_after_access_jwt_expiry(mcp_cred_env):
    """MCP auth does not read JWT exp or session validity."""
    env = mcp_cred_env
    _, session_row = create_session(
        env["employee_id"], auth_settings=env["settings"]
    )
    identity = auth_service.employee_for_session(
        session_row.id, env["employee_id"]
    )
    assert identity is not None
    expired_jwt, _ = create_access_token(
        employee_id=identity.id,
        email=identity.email,
        role=identity.role,
        permissions=sorted(identity.permissions),
        session_id=session_row.id,
        settings=env["settings"],
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=120),
    )
    # The expired employee JWT must not authenticate MCP.
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(expired_jwt)

    plaintext, _ = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"],
        name="After jwt expiry",
        created_by_employee_id=env["employee_id"],
    )
    context = execution_context_from_token(plaintext)
    assert context.employee.id == env["employee_id"]


def test_rest_jwt_authentication_still_works(mcp_cred_env):
    """Normal REST/JWT auth remains session-backed and unchanged."""
    env = mcp_cred_env
    token = _access_token(env["employee_id"], env["settings"])
    headers = {"Authorization": f"Bearer {token}"}

    # REST credential endpoints require JWT-authenticated employee, not MCP cred.
    listing = env["client"].get("/auth/mcp-credentials", headers=headers)
    assert listing.status_code == 200
    assert "credentials" in listing.json()

    # JWT still rejected by MCP path.
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(token)


def test_self_service_api_create_list_revoke_and_isolation(mcp_cred_env):
    env = mcp_cred_env
    client = env["client"]
    headers = _auth_headers(env["employee_id"], env["settings"])

    create = client.post("/auth/mcp-credentials", json={"name": "Laptop"}, headers=headers)
    assert create.status_code == 201
    body = create.json()["credential"]
    plaintext = body["credential"]
    assert plaintext.startswith("fnw_mcp_")
    cred_id = body["id"]

    listing = client.get("/auth/mcp-credentials", headers=headers)
    assert listing.status_code == 200
    creds = listing.json()["credentials"]
    assert len(creds) == 1
    assert creds[0]["id"] == cred_id
    assert "credential" not in creds[0]
    assert "token_hash" not in creds[0]

    other_headers = _auth_headers(env["other_id"], env["settings"])
    other_listing = client.get("/auth/mcp-credentials", headers=other_headers)
    assert other_listing.status_code == 200
    assert other_listing.json()["credentials"] == []

    cannot_revoke = client.post(
        f"/auth/mcp-credentials/{cred_id}/revoke", headers=other_headers
    )
    assert cannot_revoke.status_code in (403, 404)

    revoke = client.post(f"/auth/mcp-credentials/{cred_id}/revoke", headers=headers)
    assert revoke.status_code == 200
    assert revoke.json()["credential"]["revoked_at"] is not None

    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(plaintext)


def test_admin_can_list_and_revoke_employee_credentials(mcp_cred_env):
    env = mcp_cred_env
    client = env["client"]
    plaintext, record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="For admin", created_by_employee_id=env["employee_id"]
    )
    admin_headers = _auth_headers(env["admin"].id, env["settings"])

    listing = client.get(
        f"/employees/{env['employee_id']}/mcp-credentials", headers=admin_headers
    )
    assert listing.status_code == 200
    listed = listing.json()["credentials"]
    assert listed[0]["id"] == record.id
    assert "token_hash" not in str(listing.json())
    assert plaintext not in str(listing.json())
    for item in listed:
        assert "credential" not in item
        assert "token_hash" not in item

    revoke = client.post(
        f"/employees/{env['employee_id']}/mcp-credentials/{record.id}/revoke",
        headers=admin_headers,
    )
    assert revoke.status_code == 200
    assert revoke.json()["credential"]["revoked_at"] is not None
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(plaintext)


def test_deactivation_and_session_revocation_revoke_mcp_credentials(mcp_cred_env):
    env = mcp_cred_env
    p1, r1 = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="One", created_by_employee_id=env["employee_id"]
    )
    p2, r2 = mcp_credentials.create_mcp_credential(
        employee_id=env["other_id"], name="Two", created_by_employee_id=env["other_id"]
    )

    employee_service.revoke_sessions_for_employee(
        actor=env["admin"], employee_id=env["employee_id"]
    )
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(p1)
    # other employee unaffected
    assert execution_context_from_token(p2).employee.id == env["other_id"]

    # recreate and deactivate
    p3, r3 = mcp_credentials.create_mcp_credential(
        employee_id=env["other_id"], name="Three", created_by_employee_id=env["other_id"]
    )
    employee_service.deactivate_employee(actor=env["admin"], employee_id=env["other_id"])
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(p2)
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(p3)

    session = env["factory"]()
    try:
        for rid in (r1.id, r2.id, r3.id):
            row = session.query(MCPClientCredential).filter(MCPClientCredential.id == rid).one()
            assert row.revoked_at is not None
    finally:
        session.close()


def test_global_revoke_employee_sessions_does_not_revoke_mcp_credentials(mcp_cred_env):
    """Browser/session revocation is independent from MCP credentials."""
    env = mcp_cred_env
    plaintext, record = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="Global", created_by_employee_id=env["employee_id"]
    )
    auth_service.revoke_employee_sessions(env["employee_id"])
    context = execution_context_from_token(plaintext)
    assert context.employee.id == env["employee_id"]


def test_mcp_credential_auth_preserves_rbac_and_ownership(mcp_cred_env):
    """Credential resolves the same identity/permissions as the employee record."""
    env = mcp_cred_env
    jwt_token = _access_token(env["employee_id"], env["settings"])
    # REST identity is still resolved from JWT+session.
    from auth.dependencies import get_current_employee
    from fastapi.security import HTTPAuthorizationCredentials

    rest_identity = get_current_employee(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt_token)
    )
    assert rest_identity is not None

    plaintext, _ = mcp_credentials.create_mcp_credential(
        employee_id=env["employee_id"], name="Same", created_by_employee_id=env["employee_id"]
    )
    cred_ctx = execution_context_from_token(plaintext)

    assert cred_ctx.employee.id == rest_identity.id
    assert cred_ctx.employee.role == rest_identity.role
    assert cred_ctx.employee.permissions == rest_identity.permissions
    assert cred_ctx.actor.permissions == rest_identity.permissions
    assert "tasks.read" in cred_ctx.actor.permissions
