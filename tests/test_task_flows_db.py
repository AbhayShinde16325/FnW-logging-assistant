"""Regression tests for the CREATE, UPDATE and GET flows against PostgreSQL.

The tests drive the *frontend* routes (``/tasks/chat`` and
``/tasks/{task_id}``) against the real database:

* a creation inserts exactly one row,
* an update keeps the id of the task and inserts no second row,
* a read of a task that does not exist answers with the same shape as a
  read of a task that does, so no client hits a ``KeyError: 'task'``.

Every test works on rows that carry its own unique tag, and nothing is
ever deleted: the rows of a test stay in the database, so a stored record
of a real task is never removed.

The whole module is skipped when PostgreSQL is not reachable.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import app as app_module
import services.task_service as task_service
from ai.text_extractor import (
    ExtractionResult,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from database.connection import SessionLocal, create_tables
from database.models import Task


CREATE_MESSAGE = (
    "Add a task for RAM. The task is back-end optimization technique..."
)


def _database_available() -> bool:
    try:
        create_tables()
    except Exception:
        return False

    session = SessionLocal()
    try:
        session.query(Task).count()
        return True
    except SQLAlchemyError:
        return False
    finally:
        session.close()


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL") or not _database_available(),
    reason="PostgreSQL is not available.",
)


# --- helpers ---------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def marker():
    """The unique tag of the rows of one test."""

    return f"pytest-{uuid.uuid4().hex[:12]}"


def count_rows(tag: str) -> int:
    """Count the rows of one test."""

    session = SessionLocal()
    try:
        return (
            session.query(Task)
            .filter(Task.task_title.ilike(f"%{tag}%"))
            .count()
        )
    finally:
        session.close()


def insert_row(title: str, **overrides) -> int:
    values = {
        "task_title": title,
        "assigned_to": "Rahul",
        "assigned_date": date(2026, 11, 10),
        "assigned_time": time(10, 30),
        "deadline_date": date(2026, 11, 15),
        "deadline_time": time(17, 0),
        "reminder_date": date(2026, 11, 15),
        "reminder_time": time(16, 30),
        "status": "PENDING",
        "progress": 0,
    }
    values.update(overrides)

    session = SessionLocal()
    try:
        task = Task(**values)
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id
    finally:
        session.close()


def read_row(task_id: int) -> Task:
    session = SessionLocal()
    try:
        return session.query(Task).filter(Task.id == task_id).first()
    finally:
        session.close()


class ScriptedExtractor:
    """Returns canned structures instead of calling Cohere."""

    def __init__(self, extractions=None, update=None):
        self.extractions = list(extractions or [])
        self.update = update
        self.draft_calls: list[str] = []
        self.update_calls: list[str] = []

    def extract_draft(self, user_input):
        self.draft_calls.append(user_input)

        if self.extractions:
            return self.extractions.pop(0)

        return ExtractionResult()

    def extract_update(self, user_input):
        self.update_calls.append(user_input)
        return self.update


# --- 1. a creation inserts exactly one record ---------------------------------------


def test_create_through_the_chat_route_inserts_exactly_one_record(
    client, marker, monkeypatch
):
    extraction = ExtractionResult(
        task_title=f"Back-end optimization technique {marker}",
        assigned_to=marker,
        assigned_when="10 November 2026 at 10:30 AM",
        deadline_when="15 November 2026 at 5 PM",
        reminder_when="30 minutes before the deadline",
        status=None,
        progress=50,
    )

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            extractions=[
                extraction,
                extraction.model_copy(update={"status": "IN_PROGRESS"}),
            ]
        ),
    )

    first = client.post("/tasks/chat", json={"message": CREATE_MESSAGE}).json()

    assert first["intent"] == "CREATE_TASK"
    assert first["stage"] == "fields"

    second = client.post(
        "/tasks/chat",
        json={
            "message": "it is in progress",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["stage"] == "confirm_fields"

    third = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": second["stage"],
            "draft": second["draft"],
        },
    ).json()

    assert third["stage"] == "confirm_reminder"

    final = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": third["stage"],
            "draft": third["draft"],
        },
    ).json()

    assert final["status"] == "success"
    assert final["intent"] == "CREATE_TASK"
    assert final["task"]["id"]

    # exactly one row of this test was added
    assert count_rows(marker) == 1

    stored = read_row(final["task"]["id"])
    assert stored.task_title == f"Back-end optimization technique {marker}"
    assert stored.assigned_to == marker
    assert stored.status == "IN_PROGRESS"
    assert stored.progress == 50


def test_create_does_not_touch_the_existing_records(client, marker, monkeypatch):
    """A creation never looks an existing task up."""

    existing_id = insert_row(f"Business report {marker}")

    extraction = ExtractionResult(
        task_title=f"Back-end optimization technique {marker}",
        assigned_to=marker,
        assigned_when="10 November 2026 at 10:30 AM",
        deadline_when="15 November 2026 at 5 PM",
        reminder_when="30 minutes before the deadline",
        status="IN_PROGRESS",
        progress=50,
    )

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(extractions=[extraction]),
    )

    response = client.post("/tasks/chat", json={"message": CREATE_MESSAGE})

    assert response.status_code == 200
    assert response.json()["intent"] == "CREATE_TASK"

    # the existing row was not changed and no second row was written yet
    assert read_row(existing_id).status == "PENDING"
    assert read_row(existing_id).progress == 0
    assert count_rows(marker) == 1


# --- 2. an update keeps the task id ------------------------------------------------


def test_update_through_the_chat_route_keeps_the_task_id(
    client, marker, monkeypatch
):
    title = f"Business report {marker}"
    task_id = insert_row(title, assigned_to="Rahul")

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_title=title),
                changes=TaskUpdateChanges(deadline_when="17 November 2026"),
            )
        ),
    )

    response = client.post(
        "/tasks/chat",
        json={
            "message": (
                f"Update my existing task deadline to 17 November 2026, "
                f"the {title} one."
            )
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "success"
    assert body["intent"] == "UPDATE_TASK"
    # the id of the task is the same
    assert body["task"]["id"] == task_id

    # no second row was added
    assert count_rows(marker) == 1

    stored = read_row(task_id)
    assert stored.deadline_date == date(2026, 11, 17)
    # everything else survived the update
    assert stored.task_title == title
    assert stored.assigned_to == "Rahul"
    assert stored.assigned_date == date(2026, 11, 10)
    assert stored.deadline_time == time(17, 0)
    assert stored.reminder_time == time(16, 30)
    assert stored.status == "PENDING"
    assert stored.progress == 0


def test_update_of_a_task_that_does_not_exist_inserts_nothing(
    client, marker, monkeypatch
):
    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_title=f"Ghost report {marker}"),
                changes=TaskUpdateChanges(progress=70),
            )
        ),
    )

    response = client.post(
        "/tasks/chat",
        json={"message": "Update my ghost report task progress to 70%."},
    )

    assert response.status_code == 200
    assert response.json()["status"] != "success"
    assert count_rows(marker) == 0


# --- 3. a read is consistent --------------------------------------------------------


def test_read_of_a_missing_task_answers_with_the_same_shape(client, marker):
    missing_id = 999999999

    assert read_row(missing_id) is None

    response = client.get(f"/tasks/{missing_id}")

    assert response.status_code == 404

    body = response.json()

    assert body["status"] == "not_found"
    assert body["intent"] == "GET_TASK"
    assert body["task"] is None
    assert body["tasks"] == []
    assert body["message"]
    assert body["reply"] == body["message"]


def test_read_of_an_existing_task_answers_with_the_record(client, marker):
    title = f"Business report {marker}"
    task_id = insert_row(title)

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "success"
    assert body["task"]["id"] == task_id
    assert body["task"]["task_title"] == title
    assert body["task"]["deadline_date"] == "2026-11-15"
