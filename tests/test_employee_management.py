"""Employee management API and provisioning regression tests."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import joinedload, sessionmaker
from sqlalchemy.pool import StaticPool

import auth.config as auth_config
import auth.employee_service as employee_service
import auth.service as auth_service
from auth.employee_routes import router as employee_router
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_MANAGER
from auth.security import create_access_token
from auth.service import (
    bootstrap_admin,
    create_session,
    employee_for_session,
    find_or_link_google_employee,
    sync_rbac,
)
from database.connection import Base
from database.models import AuthSession, Employee, Permission, Role, RolePermission


@pytest.fixture
def employee_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Role.__table__,
        Permission.__table__,
        RolePermission.__table__,
        Employee.__table__,
        AuthSession.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(employee_service, "SessionLocal", factory)
    settings = replace(
        auth_config.load_auth_settings(),
        required=True,
        secret_key="m" * 48,
        access_token_minutes=30,
        refresh_token_days=14,
    )
    monkeypatch.setattr(auth_config, "settings", settings)
    sync_rbac()

    app = FastAPI()
    app.include_router(employee_router)
    client = TestClient(app)

    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        settings=settings,
        client=client,
        tables=tables,
        monkeypatch=monkeypatch,
    )

    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


def _add_employee(factory, *, email, role=ROLE_EMPLOYEE, name="Test Employee"):
    session = factory()

    try:
        role_row = session.query(Role).filter(Role.name == role).one()
        employee = Employee(
            name=name,
            email=email,
            role_id=role_row.id,
            is_active=True,
            google_sub=None,
        )
        session.add(employee)
        session.commit()
        session.refresh(employee)
        return int(employee.id)
    finally:
        session.close()


def _headers_for(factory, settings, employee_id):
    session = factory()

    try:
        employee = (
            session.query(Employee)
            .options(joinedload(Employee.role))
            .filter(Employee.id == employee_id)
            .one()
        )
        permissions = [
            permission.name for permission in employee.role.permissions
        ]
        email = employee.email
        role = employee.role.name
    finally:
        session.close()

    refresh_token, row = create_session(
        employee_id,
        auth_settings=settings,
    )
    access_token, _ = create_access_token(
        employee_id=employee_id,
        email=email,
        role=role,
        permissions=permissions,
        session_id=row.id,
        settings=settings,
    )

    return {"Authorization": f"Bearer {access_token}"}, row.id


def _admin(env, email="admin@example.com"):
    admin = bootstrap_admin(email=email, name="Admin")
    headers, session_id = _headers_for(env.factory, env.settings, admin.id)
    return admin, headers, session_id


def _create_via_api(env, headers, *, email, role=ROLE_EMPLOYEE, name="New Employee"):
    response = env.client.post(
        "/employees",
        headers=headers,
        json={"name": name, "email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["employee"]


def test_admin_can_create_employee(employee_env):
    _, headers, _ = _admin(employee_env)

    employee = _create_via_api(
        employee_env,
        headers,
        email="Rahul@Example.com",
        name="Rahul",
        role=ROLE_MANAGER,
    )

    assert employee["email"] == "rahul@example.com"
    assert employee["name"] == "Rahul"
    assert employee["role"] == ROLE_MANAGER
    assert employee["is_active"] is True
    assert employee["has_logged_in"] is False
    assert employee["google_linked"] is False

    session = employee_env.factory()

    try:
        row = session.query(Employee).filter(
            Employee.email == "rahul@example.com"
        ).one()
        assert row.google_sub is None
        assert row.is_active is True
    finally:
        session.close()


def test_employee_cannot_create_employees(employee_env):
    employee_id = _add_employee(
        employee_env.factory, email="employee@example.com"
    )
    headers, _ = _headers_for(
        employee_env.factory, employee_env.settings, employee_id
    )

    response = employee_env.client.post(
        "/employees",
        headers=headers,
        json={
            "name": "Unauthorized",
            "email": "unauthorized@example.com",
            "role": ROLE_EMPLOYEE,
        },
    )

    assert response.status_code == 403


def test_duplicate_email_is_rejected_case_insensitively(employee_env):
    _, headers, _ = _admin(employee_env)
    _create_via_api(
        employee_env, headers, email="Rahul@Example.com", name="Rahul"
    )

    response = employee_env.client.post(
        "/employees",
        headers=headers,
        json={
            "name": "Another Rahul",
            "email": "rahul@example.com",
            "role": ROLE_EMPLOYEE,
        },
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_invalid_role_is_rejected(employee_env):
    _, headers, _ = _admin(employee_env)

    response = employee_env.client.post(
        "/employees",
        headers=headers,
        json={
            "name": "Super User",
            "email": "super@example.com",
            "role": "SUPERADMIN",
        },
    )

    assert response.status_code == 400
    assert "Role must be one of" in response.json()["detail"]


def test_employee_can_log_in_only_after_provisioning(employee_env):
    assert find_or_link_google_employee(
        google_sub="google-sub-1",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=employee_env.settings,
    ) is None

    _, headers, _ = _admin(employee_env)
    created = _create_via_api(
        employee_env, headers, email="rahul@example.com", name="Rahul"
    )

    linked = find_or_link_google_employee(
        google_sub="google-sub-1",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=employee_env.settings,
    )

    assert linked is not None
    assert linked.id == created["id"]

    session = employee_env.factory()

    try:
        row = session.query(Employee).filter(Employee.id == created["id"]).one()
        assert row.google_sub == "google-sub-1"
    finally:
        session.close()

    assert find_or_link_google_employee(
        google_sub="different-sub",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=employee_env.settings,
    ) is None


def test_google_identity_cannot_be_reassigned_to_another_employee(employee_env):
    _, headers, _ = _admin(employee_env)
    first = _create_via_api(
        employee_env, headers, email="first@example.com", name="First"
    )
    second = _create_via_api(
        employee_env, headers, email="second@example.com", name="Second"
    )

    first_login = find_or_link_google_employee(
        google_sub="shared-sub",
        email="first@example.com",
        name="First",
        auth_settings=employee_env.settings,
    )
    assert first_login is not None
    assert first_login.id == first["id"]

    reassigned = find_or_link_google_employee(
        google_sub="shared-sub",
        email="second@example.com",
        name="Second",
        auth_settings=employee_env.settings,
    )

    assert reassigned is None

    session = employee_env.factory()

    try:
        second_row = session.query(Employee).filter(
            Employee.id == second["id"]
        ).one()
        assert second_row.google_sub is None
    finally:
        session.close()


def test_deactivated_employee_cannot_log_in_or_use_existing_session(employee_env):
    _, headers, _ = _admin(employee_env)
    created = _create_via_api(
        employee_env, headers, email="rahul@example.com", name="Rahul"
    )
    employee_headers, session_id = _headers_for(
        employee_env.factory, employee_env.settings, created["id"]
    )

    find_or_link_google_employee(
        google_sub="google-sub-1",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=employee_env.settings,
    )

    response = employee_env.client.post(
        f"/employees/{created['id']}/deactivate",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["revoked_sessions"] >= 1
    assert find_or_link_google_employee(
        google_sub="google-sub-1",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=employee_env.settings,
    ) is None
    assert employee_for_session(session_id, created["id"]) is None

    denied = employee_env.client.get("/employees", headers=employee_headers)
    assert denied.status_code == 401

    session = employee_env.factory()

    try:
        row = session.query(AuthSession).filter(
            AuthSession.id == session_id
        ).one()
        assert row.revoked_at is not None
    finally:
        session.close()


def test_role_changes_take_effect(employee_env):
    admin, admin_headers, admin_session = _admin(employee_env)
    employee_id = _add_employee(
        employee_env.factory, email="employee@example.com"
    )
    employee_headers, employee_session = _headers_for(
        employee_env.factory, employee_env.settings, employee_id
    )

    denied = employee_env.client.get("/employees", headers=employee_headers)
    assert denied.status_code == 403

    response = employee_env.client.patch(
        f"/employees/{employee_id}",
        headers=admin_headers,
        json={"role": ROLE_ADMIN},
    )
    assert response.status_code == 200
    assert response.json()["employee"]["role"] == ROLE_ADMIN

    promoted = employee_env.client.get("/employees", headers=employee_headers)
    assert promoted.status_code == 200

    assert employee_for_session(employee_session, employee_id).role == ROLE_ADMIN
    assert employee_for_session(admin_session, admin.id).role == ROLE_ADMIN


def test_deactivation_revokes_every_session(employee_env):
    admin, headers, _ = _admin(employee_env)
    target_id = _add_employee(
        employee_env.factory, email="target@example.com"
    )
    _, first_session = _headers_for(
        employee_env.factory, employee_env.settings, target_id
    )
    _, second_session = _headers_for(
        employee_env.factory, employee_env.settings, target_id
    )

    response = employee_env.client.post(
        f"/employees/{target_id}/deactivate",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["employee"]["is_active"] is False
    assert response.json()["revoked_sessions"] >= 2
    assert employee_for_session(first_session, target_id) is None
    assert employee_for_session(second_session, target_id) is None
    assert employee_for_session(_headers_for(
        employee_env.factory, employee_env.settings, admin.id
    )[1], admin.id) is not None


def test_unauthorized_users_cannot_access_management_apis(employee_env):
    anonymous = employee_env.client.get("/employees")
    assert anonymous.status_code == 401

    employee_env.monkeypatch.setattr(
        auth_config,
        "settings",
        replace(employee_env.settings, required=False),
    )
    anonymous_legacy = employee_env.client.get("/employees")
    assert anonymous_legacy.status_code == 401

    employee_id = _add_employee(
        employee_env.factory, email="employee@example.com"
    )
    employee_headers, _ = _headers_for(
        employee_env.factory, employee_env.settings, employee_id
    )
    assert employee_env.client.get(
        "/employees", headers=employee_headers
    ).status_code == 403

    manager_id = _add_employee(
        employee_env.factory, email="manager@example.com", role=ROLE_MANAGER
    )
    manager_headers, _ = _headers_for(
        employee_env.factory, employee_env.settings, manager_id
    )
    assert employee_env.client.get(
        "/employees", headers=manager_headers
    ).status_code == 200
    assert employee_env.client.post(
        "/employees",
        headers=manager_headers,
        json={
            "name": "Managed Employee",
            "email": "managed@example.com",
            "role": ROLE_EMPLOYEE,
        },
    ).status_code == 201
    assert employee_env.client.post(
        "/employees",
        headers=manager_headers,
        json={
            "name": "Escalated Admin",
            "email": "escalated@example.com",
            "role": ROLE_ADMIN,
        },
    ).status_code == 403


def test_manager_cannot_modify_or_grant_administrator_access(employee_env):
    admin, _, _ = _admin(employee_env)
    manager_id = _add_employee(
        employee_env.factory, email="manager@example.com", role=ROLE_MANAGER
    )
    manager_headers, _ = _headers_for(
        employee_env.factory, employee_env.settings, manager_id
    )
    target_id = _add_employee(
        employee_env.factory, email="target@example.com", role=ROLE_EMPLOYEE
    )

    assert employee_env.client.patch(
        f"/employees/{admin.id}",
        headers=manager_headers,
        json={"name": "Renamed Admin"},
    ).status_code == 403
    assert employee_env.client.patch(
        f"/employees/{target_id}",
        headers=manager_headers,
        json={"role": ROLE_ADMIN},
    ).status_code == 403
    assert employee_env.client.post(
        f"/employees/{admin.id}/deactivate",
        headers=manager_headers,
    ).status_code == 403
    assert employee_env.client.post(
        f"/employees/{admin.id}/revoke-sessions",
        headers=manager_headers,
    ).status_code == 403


def test_last_active_admin_cannot_be_deactivated(employee_env):
    admin, headers, _ = _admin(employee_env)

    response = employee_env.client.post(
        f"/employees/{admin.id}/deactivate",
        headers=headers,
    )

    assert response.status_code == 409
    assert "last active administrator" in response.json()["detail"]

    session = employee_env.factory()

    try:
        row = session.query(Employee).filter(Employee.id == admin.id).one()
        assert row.is_active is True
    finally:
        session.close()


def test_self_role_change_is_forbidden_even_with_another_admin(employee_env):
    admin, headers, _ = _admin(employee_env)
    _add_employee(
        employee_env.factory, email="second-admin@example.com", role=ROLE_ADMIN
    )

    response = employee_env.client.patch(
        f"/employees/{admin.id}",
        headers=headers,
        json={"role": ROLE_EMPLOYEE},
    )

    assert response.status_code == 403
    assert "cannot change your own role" in response.json()["detail"]


def test_admin_can_demote_another_admin_when_not_the_last(employee_env):
    _, headers, _ = _admin(employee_env)
    target_id = _add_employee(
        employee_env.factory, email="second-admin@example.com", role=ROLE_ADMIN
    )

    response = employee_env.client.patch(
        f"/employees/{target_id}",
        headers=headers,
        json={"role": ROLE_MANAGER},
    )

    assert response.status_code == 200
    assert response.json()["employee"]["role"] == ROLE_MANAGER


def test_list_search_filters_and_detail(employee_env):
    _, headers, _ = _admin(employee_env)
    rahul = _create_via_api(
        employee_env,
        headers,
        email="rahul@example.com",
        name="Rahul Sharma",
        role=ROLE_EMPLOYEE,
    )
    _create_via_api(
        employee_env,
        headers,
        email="priya@example.com",
        name="Priya Patel",
        role=ROLE_MANAGER,
    )
    inactive_id = _add_employee(
        employee_env.factory, email="former@example.com", name="Former Employee"
    )
    employee_env.client.post(
        f"/employees/{inactive_id}/deactivate", headers=headers
    )

    listing = employee_env.client.get("/employees", headers=headers).json()
    assert listing["total"] == 4
    assert listing["active"] == 3
    assert listing["inactive"] == 1
    assert listing["not_logged_in"] == 4
    assert set(listing["roles"]) == {ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE}

    searched = employee_env.client.get(
        "/employees", headers=headers, params={"search": "priya"}
    ).json()
    assert [row["email"] for row in searched["employees"]] == [
        "priya@example.com"
    ]
    assert searched["total"] == 4

    by_role = employee_env.client.get(
        "/employees", headers=headers, params={"role": ROLE_MANAGER}
    ).json()
    assert [row["email"] for row in by_role["employees"]] == [
        "priya@example.com"
    ]

    inactive = employee_env.client.get(
        "/employees", headers=headers, params={"status": "inactive"}
    ).json()
    assert [row["id"] for row in inactive["employees"]] == [inactive_id]

    detail = employee_env.client.get(
        f"/employees/{rahul['id']}", headers=headers
    ).json()
    assert detail["employee"]["name"] == "Rahul Sharma"


def test_revoke_sessions_endpoint_and_audit_log(employee_env, caplog):
    _, headers, _ = _admin(employee_env)

    with caplog.at_level(logging.INFO, logger="taskflow.audit"):
        created = _create_via_api(
            employee_env, headers, email="rahul@example.com", name="Rahul"
        )
        _, session_id = _headers_for(
            employee_env.factory, employee_env.settings, created["id"]
        )
        response = employee_env.client.post(
            f"/employees/{created['id']}/revoke-sessions",
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 1
    assert employee_for_session(session_id, created["id"]) is None
    assert "employee.sessions_revoked" in caplog.text
    assert "employee.created" in caplog.text


def test_missing_employee_and_activation_flow(employee_env):
    _, headers, _ = _admin(employee_env)
    missing = employee_env.client.get("/employees/9999", headers=headers)
    assert missing.status_code == 404

    employee_id = _add_employee(
        employee_env.factory, email="rahul@example.com"
    )
    deactivated = employee_env.client.post(
        f"/employees/{employee_id}/deactivate", headers=headers
    )
    assert deactivated.status_code == 200

    activated = employee_env.client.post(
        f"/employees/{employee_id}/activate", headers=headers
    )
    assert activated.status_code == 200
    assert activated.json()["employee"]["is_active"] is True
