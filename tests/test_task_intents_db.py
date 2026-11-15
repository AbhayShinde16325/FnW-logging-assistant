"""End-to-end tests against the real PostgreSQL database.

These tests exercise the complete runtime path: intent detection -> task
identification -> structured operation -> SQL validation -> SQL execution ->
result serialisation.

Every test works on its own rows, marked with a unique tag, and nothing is
ever deleted: the rows stay in the database after the test, so no stored
record of a real task is ever removed.

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
    TaskQuery,
    TaskQueryFilters,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from database.connection import SessionLocal, create_tables
from database.models import Task


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


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def marker():
    """A unique tag for the rows of one test.

    The tag is part of the title and of the assignee, so the rows of a test
    can be counted without touching the rows of another test. Nothing is
    deleted, so the tag only has to be unique.
    """

    return f"pytest-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def clean_tasks(marker):
    """The rows of one test, isolated by a unique tag.

    The fixture deliberately does *not* delete anything: a test may only
    add rows and change the rows it added itself.
    """

    yield marker


def insert_task(**overrides) -> int:
    values = {
        "task_title": "Business report",
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


def read_task(task_id: int) -> Task:
    session = SessionLocal()
    try:
        return session.query(Task).filter(Task.id == task_id).first()
    finally:
        session.close()


def count_tasks(title_like: str | None = None) -> int:
    """Count the stored rows, optionally only the ones of one test."""

    session = SessionLocal()
    try:
        query = session.query(Task)

        if title_like:
            query = query.filter(Task.task_title.ilike(f"%{title_like}%"))

        return query.count()
    finally:
        session.close()


class ScriptedExtractor:
    """Returns canned structures instead of calling Cohere."""

    def __init__(self, update=None, query=None):
        self.update = update
        self.query = query

    def extract_update(self, user_input):
        return self.update

    def extract_query(self, user_input):
        return self.query


def test_update_writes_to_the_real_database(client, clean_tasks, monkeypatch):
    title = f"Business report {clean_tasks}"
    task_id = insert_task(task_title=title, assigned_to="Rahul")

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_id=task_id),
                changes=TaskUpdateChanges(status="IN_PROGRESS", progress=70),
            )
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task progress to 70%."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["task"]["progress"] == 70
    assert body["task"]["status"] == "IN_PROGRESS"

    stored = read_task(task_id)
    assert stored.progress == 70
    assert stored.status == "IN_PROGRESS"
    # every other value survived the update
    assert stored.task_title == title
    assert stored.assigned_to == "Rahul"
    assert stored.deadline_date == date(2026, 11, 15)
    assert stored.reminder_time == time(16, 30)


def test_update_does_not_create_a_second_row(client, clean_tasks, monkeypatch):
    title = f"Business report {clean_tasks}"
    task_id = insert_task(task_title=title)

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_title=title),
                changes=TaskUpdateChanges(progress=40),
            )
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task progress to 40%."},
    )

    assert response.status_code == 200

    # only the row of this test is counted, and it is still a single row
    assert count_tasks(title_like=clean_tasks) == 1

    assert read_task(task_id).progress == 40


def test_ownership_is_enforced_in_the_database(client, clean_tasks, monkeypatch):
    title = f"Business report {clean_tasks}"
    task_id = insert_task(task_title=title, assigned_to="Rahul")

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_title=title),
                changes=TaskUpdateChanges(progress=70),
            )
        ),
    )

    response = client.post(
        "/tasks/text",
        json={
            "text": "Update my business report task progress to 70%.",
            "employee": "Someone Else",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] != "success"
    assert read_task(task_id).progress == 0


def test_read_does_not_change_the_database(client, clean_tasks, monkeypatch):
    title = f"Business report {clean_tasks}"
    task_id = insert_task(task_title=title)

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            query=TaskQuery(
                find=TaskUpdateFind(task_title=title),
                filters=TaskQueryFilters(status="PENDING"),
            )
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "What is the current status of my business report task?"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"

    stored = read_task(task_id)
    assert stored.status == "PENDING"
    assert stored.progress == 0


def test_delete_needs_the_exact_confirmation(client, clean_tasks, monkeypatch):
    """The delete gate is checked, but no record is removed.

    The confirmed deletion itself is covered by the in-memory tests, so
    that this module never removes a row from the database.
    """

    title = f"Business report {clean_tasks}"
    task_id = insert_task(task_title=title)

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            query=TaskQuery(
                find=TaskUpdateFind(task_id=task_id),
                filters=TaskQueryFilters(),
            )
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Delete my business report task."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "confirmation_required"
    assert read_task(task_id) is not None


def test_the_task_list_endpoint_returns_the_stored_rows(client, clean_tasks):
    title = f"Business report {clean_tasks}"
    insert_task(task_title=title)

    response = client.get("/tasks")

    assert response.status_code == 200
    body = response.json()

    assert body["count"] == count_tasks()
    assert body["count"] >= 1

    stored = next(task for task in body["tasks"] if task["task_title"] == title)
    assert stored["assigned_date"] == "2026-11-10"
    assert stored["reminder_time"] == "16:30"


def test_a_staged_update_is_applied_after_the_task_is_picked(
    client, clean_tasks, monkeypatch
):
    """The second turn only picks the task; the update still has to run."""

    first_id = insert_task(task_title=f"Business report {clean_tasks}")
    second_id = insert_task(task_title=f"Quarterly report {clean_tasks}")

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedExtractor(
            update=TaskUpdate(
                find=TaskUpdateFind(task_title=f"report {clean_tasks}"),
                changes=TaskUpdateChanges(progress=70),
            )
        ),
    )

    first = client.post(
        "/tasks/chat",
        json={"message": "Update my report task progress to 70%."},
    ).json()

    assert first["status"] == "task_selection_required"
    assert first["stage"] == "select_task"

    second = client.post(
        "/tasks/chat",
        json={
            "message": "the first one",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "success"

    # the two rows of this test are still the two rows of this test
    assert count_tasks(title_like=clean_tasks) == 2

    updated = [first_id, second_id]
    assert read_task(updated[0]).progress in (0, 70)
    assert read_task(updated[1]).progress in (0, 70)
    assert sum(
        1 for task_id in updated if read_task(task_id).progress == 70
    ) == 1
