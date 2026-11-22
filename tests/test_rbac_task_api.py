"""End-to-end RBAC tests for the direct task API and actor-scoped memory."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import joinedload, sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import auth.config as auth_config
import auth.employee_service as employee_service
import auth.service as auth_service
import services.conversation_memory as conversation_memory
import services.task_management as task_management
import database.task_repository as task_repository
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_MANAGER
from auth.security import create_access_token
from auth.service import bootstrap_admin, create_session, sync_rbac
from database.connection import Base
from database.models import ConversationMemory, Employee, Role, Task
from services.conversation_memory import get_conversation_memory, save_conversation_memory
from services.task_scope import TaskActor, use_task_actor


@pytest.fixture
def rbac_env(monkeypatch):
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

    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    settings = replace(
        auth_config.load_auth_settings(),
        required=True,
        secret_key="rbac-test-secret-key-that-is-long-enough-123",
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

    with TestClient(app_module.app) as client:
        yield SimpleNamespace(
            client=client,
            factory=factory,
            settings=settings,
            admin=admin,
            admin_headers=_headers(factory, settings, admin.id)[0],
            manager_id=manager_id,
            manager_headers=_headers(factory, settings, manager_id)[0],
            employee_id=employee_id,
            employee_headers=_headers(factory, settings, employee_id)[0],
            other_id=other_id,
            other_headers=_headers(factory, settings, other_id)[0],
            own_task=own_task,
            other_task=other_task,
        )

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


def _headers(factory, settings, employee_id):
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


def test_employee_lists_and_reads_only_assigned_tasks(rbac_env):
    listing = rbac_env.client.get("/tasks", headers=rbac_env.employee_headers)

    assert listing.status_code == 200
    ids = [task["id"] for task in listing.json()["tasks"]]
    assert rbac_env.own_task in ids
    assert rbac_env.other_task not in ids

    own = rbac_env.client.get(
        f"/tasks/{rbac_env.own_task}",
        headers=rbac_env.employee_headers,
    )
    other = rbac_env.client.get(
        f"/tasks/{rbac_env.other_task}",
        headers=rbac_env.employee_headers,
    )

    assert own.status_code == 200
    assert own.json()["task"]["assigned_to_employee_id"] == rbac_env.employee_id
    assert other.status_code == 404


def test_employee_can_update_only_owned_progress(rbac_env):
    updated = rbac_env.client.patch(
        f"/tasks/{rbac_env.own_task}/progress",
        headers=rbac_env.employee_headers,
        json={"progress": 40},
    )
    cross = rbac_env.client.patch(
        f"/tasks/{rbac_env.other_task}/progress",
        headers=rbac_env.employee_headers,
        json={"progress": 40},
    )

    assert updated.status_code == 200
    assert updated.json()["task"]["progress"] == 40
    assert updated.json()["task"]["status"] == "IN_PROGRESS"
    assert cross.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"progress": 50, "task_title": "Hijacked"},
        {"progress": 101},
        {"progress": -1},
        {"progress": True},
        {"progress": "50"},
        {"status": "COMPLETED"},
    ],
)
def test_employee_progress_endpoint_rejects_extra_or_invalid_fields(
    rbac_env, payload
):
    response = rbac_env.client.patch(
        f"/tasks/{rbac_env.own_task}/progress",
        headers=rbac_env.employee_headers,
        json=payload,
    )

    assert response.status_code == 422


def test_employee_can_create_edit_and_delete_own_tasks(rbac_env):
    created = rbac_env.client.post(
        "/tasks",
        headers=rbac_env.employee_headers,
        json={"task_title": "My own task"},
    )
    assert created.status_code == 201, created.text
    created_task = created.json()["task"]
    assert created_task["assigned_to_employee_id"] == rbac_env.employee_id
    assert created_task["assigned_to"] == "Rahul"
    assert created_task["created_by_employee_id"] == rbac_env.employee_id

    edited = rbac_env.client.patch(
        f"/tasks/{created_task['id']}",
        headers=rbac_env.employee_headers,
        json={
            "task_title": "My own task edited",
            "task_description": "Updated by the owner",
            "progress": 65,
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["task"]["task_title"] == "My own task edited"
    assert edited.json()["task"]["status"] == "IN_PROGRESS"
    assert edited.json()["task"]["assigned_to_employee_id"] == rbac_env.employee_id

    deleted = rbac_env.client.request(
        "DELETE",
        f"/tasks/{created_task['id']}",
        headers=rbac_env.employee_headers,
        json={"confirmation": "CONFIRM DELETE"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_task_id"] == created_task["id"]


def test_employee_cannot_assign_a_new_or_existing_task_to_somebody_else(rbac_env):
    by_name = rbac_env.client.post(
        "/tasks",
        headers=rbac_env.employee_headers,
        json={"task_title": "For Neha", "assigned_to": "Neha"},
    )
    by_id = rbac_env.client.post(
        "/tasks",
        headers=rbac_env.employee_headers,
        json={
            "task_title": "For Neha by id",
            "assigned_to_employee_id": rbac_env.other_id,
        },
    )
    reassigned = rbac_env.client.patch(
        f"/tasks/{rbac_env.own_task}",
        headers=rbac_env.employee_headers,
        json={"assigned_to_employee_id": rbac_env.other_id},
    )

    assert by_name.status_code == 403
    assert by_id.status_code == 403
    assert reassigned.status_code == 403
    assert "yourself" in by_name.json()["detail"]


def test_employee_cannot_edit_or_delete_another_employees_task(rbac_env):
    edited = rbac_env.client.patch(
        f"/tasks/{rbac_env.other_task}",
        headers=rbac_env.employee_headers,
        json={"task_title": "Hijacked"},
    )
    deleted = rbac_env.client.request(
        "DELETE",
        f"/tasks/{rbac_env.other_task}",
        headers=rbac_env.employee_headers,
        json={"confirmation": "CONFIRM DELETE"},
    )

    assert edited.status_code == 404
    assert deleted.status_code == 404

    other = rbac_env.client.get(
        f"/tasks/{rbac_env.other_task}", headers=rbac_env.admin_headers
    )
    assert other.json()["task"]["task_title"] == "Other task"


def test_manager_has_full_operational_task_access(rbac_env):
    listing = rbac_env.client.get("/tasks", headers=rbac_env.manager_headers)
    assert listing.status_code == 200
    ids = [task["id"] for task in listing.json()["tasks"]]
    assert {rbac_env.own_task, rbac_env.other_task} <= set(ids)

    created = rbac_env.client.post(
        "/tasks",
        headers=rbac_env.manager_headers,
        json={
            "task_title": "Manager task",
            "assigned_to_employee_id": rbac_env.employee_id,
            "deadline_date": "2026-12-01",
        },
    )
    assert created.status_code == 201, created.text
    created_id = created.json()["task"]["id"]
    assert created.json()["task"]["assigned_to"] == "Rahul"

    edited = rbac_env.client.patch(
        f"/tasks/{created_id}",
        headers=rbac_env.manager_headers,
        json={"task_title": "Manager task edited", "progress": 65},
    )
    assert edited.status_code == 200
    assert edited.json()["task"]["assigned_to_employee_id"] == rbac_env.employee_id

    denied_delete = rbac_env.client.request(
        "DELETE",
        f"/tasks/{created_id}",
        headers=rbac_env.manager_headers,
        json={"confirmation": "wrong"},
    )
    assert denied_delete.status_code == 400

    deleted = rbac_env.client.request(
        "DELETE",
        f"/tasks/{created_id}",
        headers=rbac_env.manager_headers,
        json={"confirmation": "CONFIRM DELETE"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted_task_id"] == created_id


def test_admin_has_full_task_access(rbac_env):
    created = rbac_env.client.post(
        "/tasks",
        headers=rbac_env.admin_headers,
        json={"task_title": "Admin task", "assigned_to": "Neha"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["id"]
    assert created.json()["task"]["assigned_to_employee_id"] == rbac_env.other_id

    edited = rbac_env.client.patch(
        f"/tasks/{task_id}",
        headers=rbac_env.admin_headers,
        json={"task_title": "Admin task edited", "progress": 100},
    )
    assert edited.status_code == 200
    assert edited.json()["task"]["status"] == "IN_PROGRESS"

    deleted = rbac_env.client.request(
        "DELETE",
        f"/tasks/{task_id}",
        headers=rbac_env.admin_headers,
        json={"confirmation": "CONFIRM DELETE"},
    )
    assert deleted.status_code == 200


def test_deactivated_employee_session_is_rejected(rbac_env):
    response = rbac_env.client.post(
        f"/employees/{rbac_env.employee_id}/deactivate",
        headers=rbac_env.admin_headers,
    )
    assert response.status_code == 200

    denied = rbac_env.client.get("/tasks", headers=rbac_env.employee_headers)
    assert denied.status_code == 401


def test_conversation_memory_ignores_caller_supplied_user_id(rbac_env):
    actor = TaskActor(
        id=rbac_env.employee_id,
        name="Rahul",
        permissions=frozenset({"tasks.read", "tasks.update"}),
    )

    with use_task_actor(actor):
        save_conversation_memory(
            "attacker-chosen-id",
            "conversation-1",
            last_action="GET_TASK",
        )
        memory = get_conversation_memory("attacker-chosen-id", "conversation-1")

    assert memory is not None
    assert memory["user_id"] == f"employee:{rbac_env.employee_id}"

    session = rbac_env.factory()

    try:
        rows = session.query(ConversationMemory).all()
        assert len(rows) == 1
        assert rows[0].employee_id == rbac_env.employee_id
    finally:
        session.close()


def test_deterministic_update_fallback_allows_own_fields_but_not_reassignment(rbac_env):
    from services.task_service import _apply_update

    actor = TaskActor(
        id=rbac_env.employee_id,
        name="Rahul",
        permissions=frozenset(
            {"tasks.create", "tasks.read", "tasks.update", "tasks.delete"}
        ),
    )

    with use_task_actor(actor):
        denied = _apply_update(
            rbac_env.own_task,
            {"assigned_to_employee_id": rbac_env.other_id},
            employee="Rahul",
        )
        allowed = _apply_update(
            rbac_env.own_task,
            {"task_title": "Renamed by fallback", "progress": 55},
            employee="Rahul",
            user_input="CONFIRM UPDATE",
        )

    assert denied["status"] == "error"
    assert "reassign" in denied["reply"]
    assert allowed["status"] == "success", allowed
    assert allowed["task"]["task_title"] == "Renamed by fallback"
    assert allowed["task"]["progress"] == 55


def test_agent_employee_writes_are_owned_and_cross_employee_attempts_fail(rbac_env):
    from services.database_agent import ToolContext, _write_query

    actor = TaskActor(
        id=rbac_env.employee_id,
        name="Rahul",
        permissions=frozenset(
            {"tasks.create", "tasks.read", "tasks.update", "tasks.delete"}
        ),
    )
    ctx = lambda confirmation=None: ToolContext(  # noqa: E731
        message="agent write",
        employee="Rahul",
        confirmation=confirmation,
    )

    with use_task_actor(actor):
        created = _write_query(
            {
                "sql": (
                    "INSERT INTO tasks (task_title, assigned_to_employee_id) "
                    "VALUES (:title, :employee_id)"
                ),
                "params": {
                    "title": "Agent-created own task",
                    "employee_id": rbac_env.employee_id,
                },
            },
            ctx(),
        )
        forged = _write_query(
            {
                "sql": (
                    "INSERT INTO tasks (task_title, assigned_to_employee_id) "
                    "VALUES (:title, :employee_id)"
                ),
                "params": {
                    "title": "Agent-created task for Neha",
                    "employee_id": rbac_env.other_id,
                },
            },
            ctx(),
        )

        created_id = created["task_id"]
        edited = _write_query(
            {
                "sql": "UPDATE tasks SET task_title = :title WHERE id = :id",
                "params": {"title": "Agent-edited own task", "id": created_id},
            },
            ctx("CONFIRM UPDATE"),
        )
        cross_edit = _write_query(
            {
                "sql": "UPDATE tasks SET task_title = :title WHERE id = :id",
                "params": {"title": "Agent hijack", "id": rbac_env.other_task},
            },
            ctx("CONFIRM UPDATE"),
        )
        cross_delete = _write_query(
            {
                "sql": "DELETE FROM tasks WHERE id = :id",
                "params": {"id": rbac_env.other_task},
            },
            ctx("CONFIRM DELETE"),
        )

    assert created["status"] == "success", created
    assert created["task"]["assigned_to_employee_id"] == rbac_env.employee_id
    assert forged["status"] == "error"
    assert "yourself" in forged["message"]
    assert edited["status"] == "success", edited
    assert cross_edit["status"] == "not_found"
    assert cross_delete["status"] == "not_found"

    own = rbac_env.client.get(
        f"/tasks/{created_id}", headers=rbac_env.employee_headers
    )
    assert own.json()["task"]["task_title"] == "Agent-edited own task"

    other = rbac_env.client.get(
        f"/tasks/{rbac_env.other_task}", headers=rbac_env.admin_headers
    )
    assert other.json()["task"]["task_title"] == "Other task"


def test_assistant_prefills_self_and_rejects_another_assignee(rbac_env, monkeypatch):
    from ai.text_extractor import ExtractionResult
    from tests.test_query_session import FakeExtractor
    import services.task_service as task_service

    actor = TaskActor(
        id=rbac_env.employee_id,
        name="Rahul",
        permissions=frozenset(
            {"tasks.create", "tasks.read", "tasks.update", "tasks.delete"}
        ),
    )
    extraction = ExtractionResult(
        task_title="Assistant task",
        task_description="Created through the assistant",
        assigned_to=None,
        assigned_when=None,
        deadline_when="25 November 2026 at 5 PM",
        reminder_when=None,
        status=None,
        progress=None,
    )
    monkeypatch.setattr(task_service, "extractor", FakeExtractor(extraction))

    with use_task_actor(actor):
        own = task_service._run_create_flow(
            "Create a task for me",
            stage="start",
            draft=None,
            answers=None,
            reminder_text=None,
            confirm=False,
        )

    monkeypatch.setattr(
        task_service,
        "extractor",
        FakeExtractor(
            extraction.model_copy(update={"assigned_to": "Neha"})
        ),
    )

    with use_task_actor(actor):
        forged = task_service._run_create_flow(
            "Create a task for Neha",
            stage="start",
            draft=None,
            answers=None,
            reminder_text=None,
            confirm=False,
        )

    assert own["status"] == "confirmation_required"
    assert own["draft"]["assigned_to"] == "Rahul"
    assert "assigned_to" not in own["missing_fields"]
    assert forged["status"] == "error"
    assert forged["reply"] == (
        "You can only create tasks assigned to yourself. "
        "I can create this task for you instead."
    )
