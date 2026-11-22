"""Refresh-session regression tests that run against PostgreSQL.

The suite uses the configured ``DATABASE_URL`` and creates unique employees
and sessions for each test. Nothing is deleted: the rows of a test stay in
the database, so unrelated records are never touched.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import auth.config as auth_config
import auth.routes as auth_routes
import auth.service as auth_service
from auth.permissions import ROLE_EMPLOYEE
from auth.security import decode_access_token
from auth.service import (
    AuthenticationError,
    ReuseDetected,
    RotatedTokenError,
    create_session,
    employee_for_session,
    rotate_refresh_session,
    sync_rbac,
)
from database.connection import Base
from database.models import AuthSession, Employee, Permission, Role, RolePermission


AUTH_TABLES = [
    Role.__table__,
    Permission.__table__,
    RolePermission.__table__,
    Employee.__table__,
    AuthSession.__table__,
]


def _database_available() -> bool:
    url = os.getenv("DATABASE_URL")

    if not url:
        return False

    engine = create_engine(url)

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        engine.dispose()


pytestmark = pytest.mark.skipif(
    not _database_available(),
    reason="PostgreSQL is not available.",
)


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    Base.metadata.create_all(engine, tables=AUTH_TABLES)

    yield engine

    engine.dispose()


@pytest.fixture
def pg_factory(pg_engine, monkeypatch):
    factory = sessionmaker(bind=pg_engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    sync_rbac()

    yield factory


@pytest.fixture
def refresh_settings():
    return replace(
        auth_config.load_auth_settings(),
        secret_key="r" * 48,
        refresh_token_days=14,
        access_token_minutes=15,
    )


def _create_employee(factory) -> int:
    session = factory()

    try:
        role = session.query(Role).filter(Role.name == ROLE_EMPLOYEE).one()
        employee = Employee(
            email=f"pytest-refresh-{uuid.uuid4().hex}@example.com",
            name="Refresh Regression",
            role=role,
            is_active=True,
        )
        session.add(employee)
        session.commit()
        session.refresh(employee)
        return int(employee.id)
    finally:
        session.close()


def _refresh_client(
    monkeypatch,
    settings,
    *,
    refresh_token: str,
    csrf_token: str,
) -> TestClient:
    monkeypatch.setattr(auth_config, "settings", settings)
    app = FastAPI()
    app.include_router(auth_routes.router)
    client = TestClient(app)
    client.cookies.set(settings.refresh_cookie_name, refresh_token)
    client.cookies.set(settings.csrf_cookie_name, csrf_token)
    return client


def test_postgres_refresh_endpoint_executes_locking_query_and_rotates(
    pg_engine,
    pg_factory,
    refresh_settings,
    monkeypatch,
):
    employee_id = _create_employee(pg_factory)
    token, old_row = create_session(
        employee_id,
        auth_settings=refresh_settings,
        user_agent="pytest-refresh",
        ip_address="127.0.0.1",
    )
    csrf_token = f"csrf-{uuid.uuid4().hex}"
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(pg_engine, "before_cursor_execute", _capture)

    try:
        client = _refresh_client(
            monkeypatch,
            refresh_settings,
            refresh_token=token,
            csrf_token=csrf_token,
        )
        response = client.post(
            "/auth/refresh",
            headers={"X-CSRF-Token": csrf_token},
        )
    finally:
        event.remove(pg_engine, "before_cursor_execute", _capture)

    assert response.status_code == 200, response.text
    payload = response.json()
    new_token = response.cookies.get(refresh_settings.refresh_cookie_name)

    assert new_token and new_token != token
    assert payload["access_token"]
    assert payload["employee"]["id"] == employee_id
    assert payload["employee"]["role"] == ROLE_EMPLOYEE
    assert "tasks.read" in payload["employee"]["permissions"]

    locking_statements = [
        statement
        for statement in statements
        if "FOR UPDATE" in statement.upper()
    ]

    assert locking_statements
    assert any(
        "FOR UPDATE OF SESSIONS" in statement.upper()
        for statement in locking_statements
    )

    session = pg_factory()

    try:
        previous = session.query(AuthSession).filter(
            AuthSession.id == old_row.id
        ).one()
        rotated = session.query(AuthSession).filter(
            AuthSession.refresh_token_hash != previous.refresh_token_hash,
            AuthSession.employee_id == employee_id,
            AuthSession.revoked_at.is_(None),
        ).one()

        assert previous.revoked_at is not None
        assert rotated.employee.id == employee_id
        assert rotated.employee.is_active is True
        assert rotated.employee.role.name == ROLE_EMPLOYEE
    finally:
        session.close()

    replay_client = _refresh_client(
        monkeypatch,
        refresh_settings,
        refresh_token=token,
        csrf_token=csrf_token,
    )
    replay = replay_client.post(
        "/auth/refresh",
        headers={"X-CSRF-Token": csrf_token},
    )

    # A duplicate replay of the just-rotated token is a benign race: the
    # server must not invalidate or revoke the active replacement session and
    # must let the racing client recover with a valid access token for it.
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["employee"]["id"] == employee_id
    assert replay_client.cookies.get(refresh_settings.refresh_cookie_name) in {
        None,
        "",
        token,
    }

    claims = decode_access_token(payload["access_token"], refresh_settings)
    replay_claims = decode_access_token(
        replay_payload["access_token"], refresh_settings
    )
    assert replay_claims["sid"] == claims["sid"]
    assert employee_for_session(claims["sid"], int(claims["sub"])) is not None
    assert employee_for_session(
        replay_claims["sid"], int(replay_claims["sub"])
    ) is not None

    session = pg_factory()

    try:
        rotated = session.query(AuthSession).filter(
            AuthSession.id == claims["sid"]
        ).one()
        assert rotated.revoked_at is None
        assert session.query(AuthSession).filter(
            AuthSession.employee_id == employee_id,
            AuthSession.revoked_at.is_(None),
        ).count() == 1
    finally:
        session.close()


def test_postgres_concurrent_refresh_only_one_request_rotates(
    pg_factory,
    refresh_settings,
):
    employee_id = _create_employee(pg_factory)
    token, _ = create_session(
        employee_id,
        auth_settings=refresh_settings,
        user_agent="pytest-concurrent",
        ip_address="127.0.0.1",
    )
    barrier = threading.Barrier(2, timeout=10)

    def _attempt():
        barrier.wait()

        try:
            return "rotated", rotate_refresh_session(
                token,
                auth_settings=refresh_settings,
            )
        except RotatedTokenError:
            return "duplicate", None
        except ReuseDetected:
            return "reuse", None
        except AuthenticationError:
            return "rejected", None
        except SQLAlchemyError as error:
            return f"error:{error.__class__.__name__}", None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _attempt(), range(2)))

    outcomes = [outcome for outcome, _ in results]
    rotated = [result for outcome, result in results if outcome == "rotated"]

    assert outcomes.count("rotated") == 1
    assert outcomes.count("duplicate") == 1
    assert "reuse" not in outcomes
    _, new_session = rotated[0]
    assert employee_for_session(new_session.id, employee_id) is not None


def test_postgres_duplicate_refresh_preserves_unrelated_sessions(
    pg_factory,
    refresh_settings,
):
    employee_id = _create_employee(pg_factory)
    token, _ = create_session(
        employee_id,
        auth_settings=refresh_settings,
    )
    _, other_session = create_session(
        employee_id,
        auth_settings=refresh_settings,
    )

    _, rotated_session = rotate_refresh_session(
        token,
        auth_settings=refresh_settings,
    )

    with pytest.raises(RotatedTokenError):
        rotate_refresh_session(token, auth_settings=refresh_settings)

    assert employee_for_session(rotated_session.id, employee_id) is not None
    assert employee_for_session(other_session.id, employee_id) is not None


def test_postgres_genuine_reuse_outside_grace_revokes_active_sessions(
    pg_factory,
    refresh_settings,
    monkeypatch,
):
    monkeypatch.setattr(auth_service, "ROTATION_DUPLICATE_GRACE_SECONDS", 0)
    employee_id = _create_employee(pg_factory)
    token, _ = create_session(
        employee_id,
        auth_settings=refresh_settings,
    )
    _, other_session = create_session(
        employee_id,
        auth_settings=refresh_settings,
    )
    _, rotated_session = rotate_refresh_session(
        token,
        auth_settings=refresh_settings,
    )

    with pytest.raises(ReuseDetected):
        rotate_refresh_session(token, auth_settings=refresh_settings)

    assert employee_for_session(rotated_session.id, employee_id) is None
    assert employee_for_session(other_session.id, employee_id) is None
