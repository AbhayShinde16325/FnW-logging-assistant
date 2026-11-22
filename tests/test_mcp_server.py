"""Protocol, authentication, authorization and transport tests for MCP."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import create_engine
from sqlalchemy.orm import joinedload, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import auth.config as auth_config
import auth.employee_service as employee_service
import auth.service as auth_service
import database.task_repository as task_repository
import mcp_server.server as server
import services.conversation_memory as conversation_memory
import services.task_management as task_management
from ai.mcp_client.client import MCPClientError, MCPToolCallingClient, MCPToolClient
from ai.tool_calling import ProviderNetworkError
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_MANAGER
from auth.security import create_access_token
from auth.service import (
    AuthenticatedEmployee,
    bootstrap_admin,
    create_session,
    sync_rbac,
)
from database.connection import Base
from database.models import Employee, Role, Task
from mcp_server.registry.tool_registry import MCPToolSpec, ToolRegistry
from mcp_server.security.authentication import MCPAuthenticationMiddleware
from mcp_server.security.execution_context import (
    MCPAuthenticationError,
    MCPExecutionContext,
    execution_context_from_token,
)
from services.task_scope import TaskActor


@pytest.fixture
def mcp_env(monkeypatch):
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
        secret_key="mcp-test-secret-key-that-is-long-enough-123",
        google_client_id="google-client",
        google_client_secret="google-secret",
        access_token_minutes=30,
        refresh_token_days=14,
    )
    monkeypatch.setattr(auth_config, "settings", settings)
    sync_rbac()

    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    manager_id = _add_employee(
        factory, email="manager@example.com", name="Manager", role=ROLE_MANAGER
    )
    employee_id = _add_employee(
        factory, email="employee@example.com", name="Rahul", role=ROLE_EMPLOYEE
    )
    other_id = _add_employee(
        factory, email="other@example.com", name="Neha", role=ROLE_EMPLOYEE
    )
    own_task = _add_task(
        factory,
        title="Own task",
        assigned_to="Rahul",
        assigned_to_employee_id=employee_id,
        created_by_employee_id=manager_id,
        progress=10,
    )
    other_task = _add_task(
        factory,
        title="Other task",
        assigned_to="Neha",
        assigned_to_employee_id=other_id,
        created_by_employee_id=manager_id,
        progress=20,
    )

    employee_context, employee_token = _context(factory, settings, employee_id)
    other_context, other_token = _context(factory, settings, other_id)
    admin_context, admin_token = _context(factory, settings, admin.id)

    try:
        yield SimpleNamespace(
            factory=factory,
            settings=settings,
            employee_id=employee_id,
            other_id=other_id,
            own_task=own_task,
            other_task=other_task,
            employee=employee_context,
            employee_token=employee_token,
            other=other_context,
            other_token=other_token,
            admin=admin_context,
            admin_token=admin_token,
        )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _add_employee(factory, *, email, name, role=ROLE_EMPLOYEE):
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


def _add_task(
    factory,
    *,
    title,
    assigned_to,
    assigned_to_employee_id,
    created_by_employee_id,
    progress=0,
):
    session = factory()

    try:
        task = Task(
            task_title=title,
            assigned_to=assigned_to,
            assigned_to_employee_id=assigned_to_employee_id,
            created_by_employee_id=created_by_employee_id,
            status="PENDING",
            progress=progress,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return int(task.id)
    finally:
        session.close()


def _context(factory, settings, employee_id):
    session = factory()

    try:
        employee = (
            session.query(Employee)
            .options(joinedload(Employee.role))
            .filter(Employee.id == employee_id)
            .one()
        )
        identity = AuthenticatedEmployee(
            id=int(employee.id),
            name=employee.name,
            email=employee.email,
            role=employee.role.name,
            permissions=frozenset(
                permission.name for permission in employee.role.permissions
            ),
            timezone=getattr(employee, "timezone", None) or "Asia/Kolkata",
        )
    finally:
        session.close()

    from auth.mcp_credentials import create_mcp_credential

    credential, _record = create_mcp_credential(
        employee_id=employee_id,
        name=f"test-{employee_id}",
        created_by_employee_id=employee_id,
    )
    return (
        MCPExecutionContext(
            employee=identity,
            actor=TaskActor.from_employee(identity),
            access_token=credential,
        ),
        credential,
    )


def _call(context, name, arguments):
    async def scenario():
        async with create_connected_server_and_client_session(
            server.mcp_server
        ) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)

    return asyncio.run(scenario())


def _payload(result):
    if result.structuredContent is not None:
        return result.structuredContent

    text = "\n".join(
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text"
    )
    return json.loads(text)


def test_employee_discovery_is_permission_filtered(mcp_env, monkeypatch):
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    async def scenario():
        async with create_connected_server_and_client_session(
            server.mcp_server
        ) as session:
            await session.initialize()
            return await session.list_tools()

    tools = asyncio.run(scenario()).tools
    names = {tool.name for tool in tools}

    assert {"create_task", "get_task", "update_task", "execute_read_query"} <= names
    assert "list_employees" not in names
    assert all(
        tool.meta["fnw/permissions"] is not None for tool in tools if tool.meta
    )


def test_employee_tools_enforce_ownership_through_mcp(mcp_env, monkeypatch):
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    own = _payload(_call(mcp_env.employee, "get_task", {"task_id": mcp_env.own_task}))
    other = _payload(
        _call(mcp_env.employee, "get_task", {"task_id": mcp_env.other_task})
    )
    listing = _payload(_call(mcp_env.employee, "list_tasks", {}))

    assert own["status"] == "success"
    assert own["task"]["assigned_to_employee_id"] == mcp_env.employee_id
    assert other["status"] == "not_found"
    assert [row["id"] for row in listing["tasks"]] == [mcp_env.own_task]

    forged = _payload(
        _call(
            mcp_env.employee,
            "create_task",
            {
                "task_title": "Forged assignment",
                "assigned_to": "Neha",
                "assigned_to_employee_id": mcp_env.other_id,
            },
        )
    )
    assert forged["status"] == "forbidden"


def test_confirmation_and_write_scope_are_enforced(mcp_env, monkeypatch):
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    pending = _payload(
        _call(
            mcp_env.other,
            "update_task_progress",
            {"task_id": mcp_env.other_task, "progress": 50, "confirmation": "nope"},
        )
    )
    assert pending["status"] == "confirmation_required"

    other_update = _payload(
        _call(
            mcp_env.employee,
            "update_task_progress",
            {
                "task_id": mcp_env.other_task,
                "progress": 50,
                "confirmation": "CONFIRM UPDATE",
            },
        )
    )
    assert other_update["status"] == "not_found"

    own_update = _payload(
        _call(
            mcp_env.employee,
            "update_task_progress",
            {
                "task_id": mcp_env.own_task,
                "progress": 40,
                "confirmation": "CONFIRM UPDATE",
            },
        )
    )
    assert own_update["status"] == "success"
    assert own_update["task"]["progress"] == 40


def test_resources_and_prompts_are_available(mcp_env, monkeypatch):
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    async def scenario():
        async with create_connected_server_and_client_session(
            server.mcp_server
        ) as session:
            await session.initialize()
            prompts = await session.list_prompts()
            resources = await session.list_resources()
            architecture = await session.read_resource("fnw://docs/architecture")
            return prompts, resources, architecture

    prompts, resources, architecture = asyncio.run(scenario())

    assert {prompt.name for prompt in prompts.prompts} == {
        "database_analysis",
        "reminder_management",
        "task_creation",
        "task_update",
    }
    assert "fnw://docs/architecture" in {
        str(resource.uri) for resource in resources.resources
    }
    assert "MCP" in architecture.contents[0].text


def test_mcp_accepts_only_dedicated_credentials(mcp_env):
    context = execution_context_from_token(mcp_env.employee_token)

    assert context.employee.id == mcp_env.employee_id
    assert context.actor.id == mcp_env.employee_id

    # Employee access JWT is rejected by MCP.
    _, session_row = create_session(
        mcp_env.employee_id, auth_settings=mcp_env.settings
    )
    identity = auth_service.employee_for_session(
        session_row.id, mcp_env.employee_id
    )
    assert identity is not None
    jwt_token, _ = create_access_token(
        employee_id=identity.id,
        email=identity.email,
        role=identity.role,
        permissions=sorted(identity.permissions),
        session_id=session_row.id,
        settings=mcp_env.settings,
    )
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(jwt_token)

    # Refresh token is rejected by MCP.
    refresh_token, _ = create_session(
        mcp_env.employee_id, auth_settings=mcp_env.settings
    )
    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(refresh_token)

    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("not-a-token")

    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token("fnw_mcp_invalid")

    with pytest.raises(MCPAuthenticationError):
        execution_context_from_token(None)


def test_unauthenticated_mcp_requests_get_401(mcp_env):
    app = FastAPI()
    app.mount("/mcp", server.mcp_asgi_app)

    response = TestClient(app).post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"] == "unauthorized"


def test_streamable_http_transport_end_to_end(mcp_env):
    app = FastAPI(lifespan=server.mcp_lifespan)
    app.mount("/mcp", server.mcp_asgi_app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()

    try:
        for _ in range(100):
            if running.started:
                break
            time.sleep(0.05)
        assert running.started

        client = MCPToolClient(
            f"http://127.0.0.1:{port}/mcp",
            mcp_env.employee_token,
        )
        names = {tool.name for tool in client.list_tools_sync()}
        profile = client.call_tool_sync("get_current_employee", {})
        forbidden = client.call_tool_sync("list_employees", {})

        assert "get_task" in names
        assert "list_employees" not in names
        assert profile["employee"]["id"] == mcp_env.employee_id
        assert forbidden["status"] == "error"

        # Employee JWT is rejected at the MCP transport boundary.
        _, session_row = create_session(
            mcp_env.employee_id, auth_settings=mcp_env.settings
        )
        identity = auth_service.employee_for_session(
            session_row.id, mcp_env.employee_id
        )
        assert identity is not None
        jwt_token, _ = create_access_token(
            employee_id=identity.id,
            email=identity.email,
            role=identity.role,
            permissions=sorted(identity.permissions),
            session_id=session_row.id,
            settings=mcp_env.settings,
        )
        with pytest.raises(MCPClientError):
            MCPToolClient(
                f"http://127.0.0.1:{port}/mcp",
                jwt_token,
            ).list_tools_sync()

        with pytest.raises(MCPClientError):
            MCPToolClient(
                f"http://127.0.0.1:{port}/mcp",
                "invalid-token",
            ).list_tools_sync()
    finally:
        running.should_exit = True
        thread.join(timeout=5)


def test_cohere_adapter_discovers_and_routes_mcp_tools(mcp_env):
    calls = []
    provider = SimpleNamespace(
        message_adapter="cohere",
        chat=lambda *, messages, tools: SimpleNamespace(tools=tools),
    )

    class FakeMCP:
        def list_tools_sync(self):
            return [
                SimpleNamespace(
                    name="get_task",
                    description="Get a task",
                    inputSchema={"type": "object", "properties": {}},
                    meta={"fnw/permissions": ["tasks.read"]},
                ),
                SimpleNamespace(
                    name="list_employees",
                    description="List employees",
                    inputSchema={"type": "object", "properties": {}},
                    meta={"fnw/permissions": ["employees.read"]},
                ),
            ]

        def call_tool_sync(self, name, arguments):
            calls.append((name, arguments))
            return {"status": "confirmation_required"}

    client = MCPToolCallingClient(
        provider,
        server_url="http://mcp.test/mcp",
        access_token=mcp_env.employee_token,
        actor=mcp_env.employee.actor,
        cache_seconds=0,
    )
    client._mcp = FakeMCP()

    definitions = client.tool_definitions()
    assert [tool["function"]["name"] for tool in definitions] == ["get_task"]

    client.call_tool(
        "update_task_progress",
        {"task_id": 1, "progress": 5, "confirmation": "forged"},
        trusted_confirmation="CONFIRM UPDATE",
    )
    assert calls == [
        (
            "update_task_progress",
            {"task_id": 1, "progress": 5, "confirmation": "CONFIRM UPDATE"},
        )
    ]

    client.call_tool(
        "update_task_progress",
        {"task_id": 1, "progress": 5},
        trusted_confirmation=None,
    )
    assert calls[-1][1]["confirmation"] is None


def test_mcp_discovery_failure_is_a_provider_network_error(mcp_env):
    provider = SimpleNamespace(message_adapter="cohere")
    client = MCPToolCallingClient(
        provider,
        server_url="http://mcp.test/mcp",
        access_token=mcp_env.employee_token,
        actor=mcp_env.employee.actor,
        cache_seconds=0,
    )

    def fail():
        raise MCPClientError("server down")

    client._mcp.list_tools_sync = fail

    with pytest.raises(ProviderNetworkError):
        client.chat(messages=[], tools=None)


def test_registry_rejects_duplicate_tool_names(mcp_env):
    registry = ToolRegistry()
    spec = MCPToolSpec(
        name="get_task",
        description="Get a task",
        input_schema={"type": "object", "properties": {}},
        permissions=frozenset({"tasks.read"}),
        handler=lambda arguments, context: {},
    )
    registry.register(spec)

    with pytest.raises(ValueError):
        registry.register(spec)


def test_mcp_credential_works_after_session_logout(mcp_env, monkeypatch):
    """MCP auth is independent of browser/session logout and logout-all."""
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    # Single-session logout equivalent.
    refresh_token, session_row = auth_service.create_session(
        mcp_env.employee_id, auth_settings=mcp_env.settings
    )
    assert auth_service.revoke_refresh_session(refresh_token) is True
    assert (
        auth_service.employee_for_session(session_row.id, mcp_env.employee_id)
        is None
    )

    # Logout-all / revoke-all-sessions equivalent.
    auth_service.revoke_employee_sessions(mcp_env.employee_id)

    # Same MCP credential must still authenticate and resolve the same employee.
    context = execution_context_from_token(mcp_env.employee_token)
    assert context.employee.id == mcp_env.employee_id
    profile = _payload(_call(context, "get_current_employee", {}))
    assert profile["status"] == "success"
    assert profile["employee"]["id"] == mcp_env.employee_id


def test_mcp_tools_use_same_database_as_application(mcp_env, monkeypatch):
    """Reads/writes through MCP go through the same repository/session factory."""
    monkeypatch.setattr(server, "_execution_context", lambda: mcp_env.employee)

    created = _payload(
        _call(
            mcp_env.employee,
            "create_task",
            {"task_title": "MCP DB parity"},
        )
    )
    assert created["status"] == "success"
    task_id = created["task"]["id"]

    # Visible through the application repository using the same patched factory.
    from services.task_scope import use_task_actor

    with use_task_actor(mcp_env.employee.actor):
        task = task_repository.get_task(task_id)
    assert task is not None
    assert task.task_title == "MCP DB parity"
