from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import auth.mcp_credentials as mc
from auth.security import utcnow
from database.connection import Base
from database.models import Employee, MCPClientCredential, Role
from mcp_server.security.execution_context import execution_context_from_token


@pytest.fixture
def bootstrap_env(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    session = factory()
    try:
        admin = Role(id=1, name="ADMIN", description="admin")
        emp_role = Role(id=3, name="EMPLOYEE", description="employee")
        session.add_all([admin, emp_role])
        session.commit()
        session.add_all(
            [
                Employee(
                    id=1,
                    name="Shreyansh Dixit",
                    email="shreyansh@example.com",
                    role_id=1,
                    is_active=True,
                ),
                Employee(
                    id=2,
                    name="Other Employee",
                    email="other@example.com",
                    role_id=3,
                    is_active=True,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(mc, "SessionLocal", factory)
    cred_file = tmp_path / "fnw-mcp-authorization"
    token = "fnw_mcp_test_bootstrap_credential_1234567890ABCDE"
    cred_file.write_text(f"Bearer {token}\n", encoding="utf-8")

    monkeypatch.setenv("MCP_BOOTSTRAP_CREDENTIAL_FILE", str(cred_file))
    monkeypatch.setenv("MCP_BOOTSTRAP_EMPLOYEE_ID", "1")

    return {
        "factory": factory,
        "token": token,
        "cred_file": cred_file,
        "monkeypatch": monkeypatch,
    }


def test_bootstrap_registers_missing_credential(bootstrap_env):
    env = bootstrap_env
    record = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert record is not None
    assert record.employee_id == 1
    assert record.name == mc.BOOTSTRAP_CREDENTIAL_NAME
    assert record.revoked_at is None
    assert record.expires_at is None

    session = env["factory"]()
    try:
        rows = session.query(MCPClientCredential).all()
        assert len(rows) == 1
        assert rows[0].token_hash == mc.hash_credential(env["token"])
        assert env["token"] not in (rows[0].token_hash or "")
    finally:
        session.close()


def test_bootstrap_is_idempotent_across_restarts(bootstrap_env):
    env = bootstrap_env
    first = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    second = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert first is not None and second is not None
    assert first.id == second.id

    session = env["factory"]()
    try:
        assert session.query(MCPClientCredential).count() == 1
    finally:
        session.close()


def test_bootstrap_does_not_reactivate_revoked_credential(bootstrap_env):
    env = bootstrap_env
    first = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert first is not None

    session = env["factory"]()
    try:
        row = session.query(MCPClientCredential).filter(MCPClientCredential.id == first.id).one()
        row.revoked_at = utcnow() - timedelta(minutes=5)
        session.commit()
    finally:
        session.close()

    second = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert second is not None
    assert second.id == first.id
    assert second.revoked_at is not None

    session = env["factory"]()
    try:
        rows = session.query(MCPClientCredential).all()
        assert len(rows) == 1
        assert rows[0].revoked_at is not None
    finally:
        session.close()


def test_bootstrap_does_not_recreate_expired_credential(bootstrap_env):
    env = bootstrap_env
    first = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert first is not None

    session = env["factory"]()
    try:
        row = session.query(MCPClientCredential).filter(MCPClientCredential.id == first.id).one()
        row.expires_at = utcnow() - timedelta(minutes=5)
        session.commit()
    finally:
        session.close()

    second = mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    assert second is not None
    assert second.id == first.id
    assert second.expires_at is not None

    session = env["factory"]()
    try:
        rows = session.query(MCPClientCredential).all()
        assert len(rows) == 1
        assert rows[0].expires_at is not None
        assert rows[0].revoked_at is None
    finally:
        session.close()


def test_bootstrap_fails_on_employee_mapping_mismatch(bootstrap_env):
    env = bootstrap_env
    session = env["factory"]()
    try:
        session.add(
            MCPClientCredential(
                id="cred-other",
                employee_id=2,
                name="other",
                token_prefix=env["token"][:20],
                token_hash=mc.hash_credential(env["token"]),
                created_at=utcnow(),
                created_by_employee_id=2,
                expires_at=None,
                revoked_at=None,
                last_used_at=None,
            )
        )
        session.commit()
    finally:
        session.close()

    with pytest.raises(mc.MCPCredentialError, match="different employee"):
        mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])


def test_bootstrap_fails_when_employee_inactive(bootstrap_env):
    env = bootstrap_env
    session = env["factory"]()
    try:
        emp = session.query(Employee).filter(Employee.id == 1).one()
        emp.is_active = False
        session.commit()
    finally:
        session.close()

    with pytest.raises(mc.MCPCredentialError, match="inactive"):
        mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])


def test_bootstrap_fails_when_employee_missing(bootstrap_env):
    env = bootstrap_env
    env["monkeypatch"].setenv("MCP_BOOTSTRAP_EMPLOYEE_ID", "999")
    with pytest.raises(mc.MCPCredentialError, match="not found"):
        mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])


def test_bootstrap_credential_authenticates_as_employee(bootstrap_env):
    env = bootstrap_env
    mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])
    ctx = execution_context_from_token(env["token"])
    assert ctx.employee.id == 1
    assert ctx.employee.email == "shreyansh@example.com"


def test_bootstrap_missing_file_fails_safely(bootstrap_env):
    env = bootstrap_env
    env["monkeypatch"].setenv("MCP_BOOTSTRAP_CREDENTIAL_FILE", str(env["cred_file"]) + ".missing")
    with pytest.raises(mc.MCPCredentialError, match="not found"):
        mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"])


def test_bootstrap_noop_when_not_configured(bootstrap_env):
    env = bootstrap_env
    env["monkeypatch"].delenv("MCP_BOOTSTRAP_CREDENTIAL_FILE", raising=False)
    env["monkeypatch"].delenv("MCP_BOOTSTRAP_CREDENTIAL", raising=False)
    assert mc.reconcile_bootstrap_mcp_credential(session_factory=env["factory"]) is None
