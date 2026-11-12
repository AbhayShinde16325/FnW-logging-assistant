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
    monkeypatch.setattr(
        task_service,
        "create_task",
        lambda record: {
            "id": 1,
            "task_title": record.task_title,
            "assigned_to": record.assigned_to,
            "status": record.status,
            "progress": record.progress,
            "reminder_date": str(record.reminder_date),
            "reminder_time": record.reminder_time.strftime("%H:%M"),
        },
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_text_start_requires_non_empty(client, staged_service):
    response = client.post("/tasks/text", json={"text": "   "})
    assert response.status_code == 200
    assert response.json()["status"] == "error"


def test_staged_create_flow_end_to_end(client, staged_service):
    # Stage 1: start -> one field (status) is missing
    response = client.post(
        "/tasks/text",
        json={"text": "Rahul has a Python API research task", "stage": "start"},
    )
    assert response.status_code == 200

    first = response.json()
    assert first["status"] == "clarification_required"
    assert first["stage"] == "fields"
    assert "status" in first["missing_fields"]
    draft = first["draft"]

    # Stage 2: answer the missing field
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

    # Stage 3: confirm the 8 fields -> reminder is asked
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

    third = response.json()
    assert third["status"] == "reminder_required"
    assert third["stage"] == "reminder"
    draft = third["draft"]

    # Stage 4: relative reminder -> absolute confirmation
    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "reminder",
            "draft": draft,
            "reminder_text": "30 minutes before the deadline",
        },
    )
    assert response.status_code == 200

    fourth = response.json()
    assert fourth["status"] == "reminder_confirmation_required"
    assert fourth["reminder"]["date"] == "2026-11-15"
    assert fourth["reminder"]["time"] == "16:30"
    draft = fourth["draft"]

    # Stage 5: final confirmation -> task saved
    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "confirm_reminder",
            "draft": draft,
            "confirm": True,
        },
    )
    assert response.status_code == 200

    final = response.json()
    assert final["status"] == "success"
    assert final["task"]["status"] == "IN_PROGRESS"
    assert final["task"]["reminder_date"] == "2026-11-15"
    assert final["task"]["reminder_time"] == "16:30"


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


def test_update_flow_unchanged(client, staged_service, monkeypatch):
    # UPDATE keeps the single-shot behaviour: fake it end to end.
    class UpdateExtractor(FakeExtractor):
        def extract_task(self, user_input):
            from ai.text_extractor import TaskDetails

            return TaskDetails(
                task_title="Python API research",
                assigned_to="Rahul",
                assigned_date="2026-11-10",
                assigned_time="10:30",
                deadline_date="2026-11-15",
                deadline_time="17:00",
                status="IN_PROGRESS",
                progress=80,
            )

    monkeypatch.setattr(task_service, "extractor", UpdateExtractor(full_extraction()))
    monkeypatch.setattr(
        task_service,
        "update_task",
        lambda task_id, updates: {"id": task_id, "progress": updates["progress"]},
    )

    response = client.post(
        "/tasks/text",
        json={
            "text": "Update the Python API research task, now 80 percent done",
            "task_id": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["task"]["progress"] == 80
