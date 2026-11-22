from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth.config as auth_config
import auth.service as auth_service
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE
from auth.service import (
    AuthenticationError,
    ReuseDetected,
    RotatedTokenError,
    bootstrap_admin,
    create_session,
    employee_for_session,
    find_or_link_google_employee,
    revoke_employee_sessions,
    revoke_refresh_session,
    rotate_refresh_session,
    sync_rbac,
)
from database.connection import Base
from database.models import AuthSession, Employee, Permission, Role, RolePermission


@pytest.fixture
def auth_db(monkeypatch):
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
    sync_rbac()

    yield factory

    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()


def test_sync_rbac_is_idempotent_and_seeds_canonical_roles(auth_db):
    sync_rbac()
    sync_rbac()

    session = auth_db()

    try:
        roles = {row.name for row in session.query(Role).all()}
        permissions = {row.name for row in session.query(Permission).all()}
        admin_links = session.query(RolePermission).join(Role).filter(
            Role.name == ROLE_ADMIN
        ).count()
    finally:
        session.close()

    assert roles == {"ADMIN", "MANAGER", "EMPLOYEE"}
    assert "roles.manage" in permissions
    assert "tasks.read_all" in permissions
    assert admin_links == len(permissions)


def test_bootstrap_admin_creates_and_promotes_without_duplicates(auth_db):
    created = bootstrap_admin(email="Admin@Example.com", name="Admin")
    promoted = bootstrap_admin(email="admin@example.com", name="Administrator")

    assert created.id == promoted.id
    assert promoted.role == ROLE_ADMIN
    assert "roles.manage" in promoted.permissions

    session = auth_db()

    try:
        assert session.query(Employee).count() == 1
        employee = session.query(Employee).one()
        assert employee.email == "admin@example.com"
        assert employee.name == "Administrator"
    finally:
        session.close()


def test_google_identity_links_only_the_matching_active_employee(auth_db):
    bootstrap_admin(email="admin@example.com", name="Admin")
    session = auth_db()
    session.add(
        Employee(
            email="rahul@example.com",
            name="Rahul",
            role_id=session.query(Role).filter(Role.name == ROLE_EMPLOYEE).one().id,
            is_active=True,
        )
    )
    session.commit()
    session.close()

    linked = find_or_link_google_employee(
        google_sub="google-subject",
        email="RAHUL@example.com",
        name="Rahul",
        auth_settings=replace(auth_config.load_auth_settings(), employee_domain=None),
    )

    assert linked is not None
    assert linked.email == "rahul@example.com"

    again = find_or_link_google_employee(
        google_sub="google-subject",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=replace(auth_config.load_auth_settings(), employee_domain=None),
    )
    assert again is not None

    different_subject = find_or_link_google_employee(
        google_sub="other-subject",
        email="rahul@example.com",
        name="Rahul",
        auth_settings=replace(auth_config.load_auth_settings(), employee_domain=None),
    )
    assert different_subject is None


def test_google_identity_respects_domain_and_inactive_employees(auth_db):
    bootstrap_admin(email="admin@example.com", name="Admin")
    session = auth_db()
    session.add(
        Employee(
            email="former@example.com",
            name="Former",
            role_id=session.query(Role).filter(Role.name == ROLE_EMPLOYEE).one().id,
            is_active=False,
        )
    )
    session.commit()
    session.close()

    settings = replace(
        auth_config.load_auth_settings(),
        employee_domain="example.com",
    )

    assert find_or_link_google_employee(
        google_sub="sub-1",
        email="not-at-work@gmail.com",
        name="Other",
        auth_settings=settings,
    ) is None
    assert find_or_link_google_employee(
        google_sub="sub-2",
        email="former@example.com",
        name="Former",
        auth_settings=settings,
    ) is None


def test_duplicate_refresh_of_rotated_token_preserves_active_sessions(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(
        auth_config.load_auth_settings(),
        secret_key="d" * 48,
        refresh_token_days=14,
    )
    first_token, first_session = create_session(admin.id, auth_settings=settings)
    second_token, second_session = create_session(admin.id, auth_settings=settings)

    rotated_token, rotated_session = rotate_refresh_session(
        first_token,
        auth_settings=settings,
    )

    assert rotated_token != first_token
    assert rotated_session.id != first_session.id
    assert employee_for_session(rotated_session.id, admin.id) is not None

    # A concurrent/duplicate request replaying the old token while its
    # replacement session is active must not revoke unrelated sessions.
    with pytest.raises(RotatedTokenError):
        rotate_refresh_session(first_token, auth_settings=settings)

    assert employee_for_session(rotated_session.id, admin.id) is not None
    assert employee_for_session(second_session.id, admin.id) is not None

    session = auth_db()

    try:
        revoked = {
            row.id: row.revoked_at
            for row in session.query(AuthSession).all()
        }
        previous = session.query(AuthSession).filter(
            AuthSession.id == first_session.id
        ).one()
    finally:
        session.close()

    assert revoked[first_session.id] is not None
    assert revoked[second_session.id] is None
    assert revoked[rotated_session.id] is None
    assert previous.replaced_by_session_id == rotated_session.id


def test_genuine_reuse_outside_grace_revokes_every_active_session(auth_db, monkeypatch):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(
        auth_config.load_auth_settings(),
        secret_key="d2" * 24,
        refresh_token_days=14,
    )
    monkeypatch.setattr(auth_service, "ROTATION_DUPLICATE_GRACE_SECONDS", 0)

    first_token, first_session = create_session(admin.id, auth_settings=settings)
    _, second_session = create_session(admin.id, auth_settings=settings)
    _, rotated_session = rotate_refresh_session(first_token, auth_settings=settings)

    with pytest.raises(ReuseDetected):
        rotate_refresh_session(first_token, auth_settings=settings)

    assert employee_for_session(rotated_session.id, admin.id) is None
    assert employee_for_session(second_session.id, admin.id) is None

    session = auth_db()

    try:
        rows = {row.id: row for row in session.query(AuthSession).all()}
    finally:
        session.close()

    assert rows[first_session.id].revoked_at is not None
    assert rows[second_session.id].revoked_at is not None
    assert rows[rotated_session.id].revoked_at is not None


def test_logout_revokes_one_session_and_logout_all_revokes_the_rest(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(auth_config.load_auth_settings(), secret_key="e" * 48)
    token_one, session_one = create_session(admin.id, auth_settings=settings)
    token_two, session_two = create_session(admin.id, auth_settings=settings)

    assert revoke_refresh_session(token_one) is True
    assert revoke_refresh_session(token_one) is False
    assert employee_for_session(session_one.id, admin.id) is None
    assert employee_for_session(session_two.id, admin.id) is not None

    assert revoke_employee_sessions(admin.id) == 1
    assert employee_for_session(session_two.id, admin.id) is None


def test_deactivated_employee_cannot_refresh(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(auth_config.load_auth_settings(), secret_key="f" * 48)
    token, _ = create_session(admin.id, auth_settings=settings)

    session = auth_db()

    try:
        employee = session.query(Employee).filter(Employee.id == admin.id).one()
        employee.is_active = False
        session.commit()
    finally:
        session.close()

    with pytest.raises(AuthenticationError):
        rotate_refresh_session(token, auth_settings=settings)


def test_expired_refresh_token_is_revoked(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(auth_config.load_auth_settings(), secret_key="g" * 48)
    token, session_row = create_session(admin.id, auth_settings=settings)

    session = auth_db()

    try:
        row = session.query(AuthSession).filter(
            AuthSession.id == session_row.id
        ).one()
        row.expires_at = row.created_at
        session.commit()
    finally:
        session.close()

    with pytest.raises(AuthenticationError):
        rotate_refresh_session(token, auth_settings=settings)


def test_valid_refresh_token_rotates_and_loads_employee_and_role(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(auth_config.load_auth_settings(), secret_key="h" * 48)
    token, old_row = create_session(admin.id, auth_settings=settings)

    new_token, new_row = rotate_refresh_session(token, auth_settings=settings)

    assert new_token != token
    assert new_row.id != old_row.id
    assert new_row.employee_id == admin.id

    session = auth_db()

    try:
        previous = session.query(AuthSession).filter(
            AuthSession.id == old_row.id
        ).one()
        rotated = session.query(AuthSession).filter(
            AuthSession.id == new_row.id
        ).one()

        assert previous.revoked_at is not None
        assert rotated.revoked_at is None
        assert rotated.employee.id == admin.id
        assert rotated.employee.is_active is True
        assert rotated.employee.role.name == ROLE_ADMIN
        assert "roles.manage" in {
            permission.name for permission in rotated.employee.role.permissions
        }
    finally:
        session.close()


def test_explicitly_revoked_session_cannot_refresh(auth_db):
    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    settings = replace(auth_config.load_auth_settings(), secret_key="i" * 48)
    token, first_session = create_session(admin.id, auth_settings=settings)
    _, other_session = create_session(admin.id, auth_settings=settings)

    assert revoke_refresh_session(token) is True

    # Revoked without a replacement session is genuine reuse, not a benign
    # duplicate of a successful rotation.
    with pytest.raises(ReuseDetected):
        rotate_refresh_session(token, auth_settings=settings)

    assert employee_for_session(other_session.id, admin.id) is None
