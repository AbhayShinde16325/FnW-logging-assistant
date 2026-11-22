"""Tests for services/task_service.py staged CREATE flow via the FastAPI app.

The database lifespan hook is stubbed and the repository's create_task is
monkeypatched, so no PostgreSQL or Cohere access is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
from ai.query_session import TaskDraftEngine  # noqa: F401 (import sanity)
from ai.text_extractor import ExtractionResult
from tests.test_query_session import FakeExtractor, full_extraction


@pytest.fixture()
def client(monkeypatch):
    # Avoid the real database during app startup.
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def staged_service(monkeypatch):
    """Service wired to a fake extractor and a fake create_task."""
    monkeypatch.setattr(task_service, "extractor", FakeExtractor(full_extraction()))
    def fake_create(record):
        return {
            "id": 1,
            "task_title": record.task_title,
            "task_description": record.task_description,
            "assigned_to": record.assigned_to,
            "status": record.status,
            "progress": record.progress,
            "reminder_date": (
                str(record.reminder_date) if record.reminder_date else None
            ),
            "reminder_time": (
                record.reminder_time.strftime("%H:%M")
                if record.reminder_time
                else None
            ),
        }

    monkeypatch.setattr(task_service, "create_task", fake_create)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_text_start_requires_non_empty(client, staged_service):
    response = client.post("/tasks/text", json={"text": "   "})
    assert response.status_code == 200
    assert response.json()["status"] == "error"


def test_staged_create_flow_end_to_end(client, staged_service):
    # Stage 1: the required fields are present, so the task is ready to confirm.
    response = client.post(
        "/tasks/text",
        json={"text": "Rahul has a Python API research task", "stage": "start"},
    )
    assert response.status_code == 200

    first = response.json()
    assert first["status"] == "confirmation_required"
    assert first["stage"] == "confirm_fields"
    assert first["missing_fields"] == []
    assert first["draft"]["task_description"] == "Summarise the API findings"
    draft = first["draft"]

    # Stage 2: an optional field can still be edited before saving.
    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "clarify",
            "draft": draft,
            "answers": {"status": "IN_PROGRESS"},
        },
    )
    assert response.status_code == 200

    second = response.json()
    assert second["status"] == "confirmation_required"
    assert second["stage"] == "confirm_fields"
    draft = second["draft"]

    # Stage 3: confirmation asks for the optional reminder.
    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "confirm_fields",
            "draft": draft,
            "confirm": True,
        },
    )
    assert response.status_code == 200

    reminder = response.json()
    assert reminder["status"] == "reminder_required"
    assert reminder["stage"] == "reminder"
    draft = reminder["draft"]

    # Stage 4: no reminder is required, so the task is saved without one.
    response = client.post(
        "/tasks/text",
        json={
            "text": "no reminder",
            "stage": "reminder",
            "draft": draft,
        },
    )
    assert response.status_code == 200

    final = response.json()
    assert final["status"] == "success"
    assert final["task"]["status"] == "IN_PROGRESS"
    assert final["task"]["task_description"] == "Summarise the API findings"
    assert final["task"]["reminder_date"] is None
    assert final["task"]["reminder_time"] is None


def test_edit_during_confirmation_round_trip(client, staged_service):
    response = client.post(
        "/tasks/text",
        json={"text": "Rahul has a Python API research task", "stage": "start"},
    )
    draft = response.json()["draft"]

    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "clarify",
            "draft": draft,
            "answers": {"status": "IN_PROGRESS"},
        },
    )
    draft = response.json()["draft"]

    # Edit progress at the confirmation stage
    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "confirm_fields",
            "draft": draft,
            "answers": {"progress": 80},
        },
    )
    edited = response.json()
    assert edited["status"] == "confirmation_required"
    assert edited["draft"]["progress"] == 80


def test_update_flow_changes_the_existing_task(client, staged_service, monkeypatch):
    """An UPDATE request must change the stored row, never create one."""

    from datetime import date, time
    from types import SimpleNamespace

    from ai.text_extractor import TaskUpdate, TaskUpdateChanges, TaskUpdateFind

    task = SimpleNamespace(
        id=1,
        task_title="Python API research",
        assigned_to="Rahul",
        assigned_date=date(2026, 11, 10),
        assigned_time=time(10, 30),
        deadline_date=date(2026, 11, 15),
        deadline_time=time(17, 0),
        reminder_date=None,
        reminder_time=None,
        status="IN_PROGRESS",
        progress=50,
    )

    class UpdateExtractor(FakeExtractor):
        def extract_update(self, user_input):
            return TaskUpdate(
                find=TaskUpdateFind(task_id=1),
                changes=TaskUpdateChanges(status="COMPLETED", progress=100),
            )

    def fake_run(operation, *, confirmation=None):
        for key, value in operation["updates"].items():
            setattr(task, key, value)
        return {"status": "success", "operation": "UPDATE", "rows": []}

    monkeypatch.setattr(task_service, "extractor", UpdateExtractor(full_extraction()))
    monkeypatch.setattr(
        task_service, "get_task", lambda task_id: task if task_id == 1 else None
    )
    monkeypatch.setattr(task_service, "run_sql_operation", fake_run)

    response = client.post(
        "/tasks/text",
        json={
            "text": "Update my Python API research task, now 100 percent done",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["intent"] == "UPDATE_TASK"
    assert body["task"]["status"] == "COMPLETED"
    assert body["task"]["progress"] == 100
    assert task.progress == 100


def test_chat_asks_one_field_at_a_time_and_updates_draft(client, staged_service):
    response = client.post(
        "/tasks/chat",
        json={
            "message": "Rahul has a Python API research task",
            "stage": "start",
        },
    )
    assert response.status_code == 200

    first = response.json()
    assert first["status"] == "confirmation_required"
    assert first["missing_fields"] == []
    assert first["draft"]["task_title"] == "Python API research"
    assert first["draft"]["task_description"] == "Summarise the API findings"

    response = client.post(
        "/tasks/chat",
        json={
            "message": "it is in progress",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    )
    second = response.json()
    assert second["status"] == "confirmation_required"
    assert second["draft"]["status"] == "IN_PROGRESS"

    response = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": second["stage"],
            "draft": second["draft"],
        },
    )
    reminder = response.json()
    assert reminder["status"] == "reminder_required"

    response = client.post(
        "/tasks/chat",
        json={
            "message": "no reminder",
            "stage": reminder["stage"],
            "draft": reminder["draft"],
        },
    )
    final = response.json()
    assert final["status"] == "success"
    assert final["reply"] == "Task created successfully."


def test_chat_syncs_pending_status_with_progress(client, staged_service):
    response = client.post(
        "/tasks/chat",
        json={
            "message": "Rahul has a Python API research task",
            "stage": "start",
        },
    )
    first = response.json()
    assert first["missing_fields"] == []
    assert first["draft"]["progress"] == 50

    response = client.post(
        "/tasks/chat",
        json={
            "message": "pending",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    )
    second = response.json()

    assert second["status"] == "confirmation_required"
    assert second["draft"]["status"] == "IN_PROGRESS"
    assert second["draft"]["progress"] == 50


def test_chat_escapes_loop_when_all_fields_are_filled(client, staged_service):
    response = client.post(
        "/tasks/chat",
        json={
            "message": "Rahul has a Python API research task",
            "stage": "start",
        },
    )
    first = response.json()
    draft = first["draft"]
    draft["status"] = "PENDING"

    response = client.post(
        "/tasks/chat",
        json={
            "message": "IN_PROGRESS",
            "stage": "fields",
            "draft": draft,
        },
    )
    second = response.json()

    assert second["status"] == "confirmation_required"
    assert second["draft"]["status"] == "IN_PROGRESS"
    assert "must be IN_PROGRESS" not in second["reply"]


def test_list_tasks_returns_all_task_details(client, monkeypatch):
    from datetime import date, time
    from types import SimpleNamespace

    sample = SimpleNamespace(
        id=7,
        task_title="Python API research",
        assigned_to="Rahul",
        assigned_date=date(2026, 11, 10),
        assigned_time=time(10, 30),
        deadline_date=date(2026, 11, 15),
        deadline_time=time(17, 0),
        reminder_date=date(2026, 11, 15),
        reminder_time=time(16, 30),
        status="IN_PROGRESS",
        progress=50,
    )

    monkeypatch.setattr(app_module, "get_all_tasks", lambda: [sample])

    response = client.get("/tasks")
    assert response.status_code == 200

    body = response.json()
    assert body["count"] == 1

    task = body["tasks"][0]
    assert task["id"] == 7
    assert task["task_title"] == "Python API research"
    assert task["assigned_date"] == "2026-11-10"
    assert task["assigned_time"] == "10:30"
    assert task["deadline_date"] == "2026-11-15"
    assert task["deadline_time"] == "17:00"
    assert task["reminder_date"] == "2026-11-15"
    assert task["reminder_time"] == "16:30"
    assert task["status"] == "IN_PROGRESS"
    assert task["progress"] == 50
