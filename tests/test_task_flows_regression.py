"""Regression tests for the CREATE, UPDATE and GET flows.

These tests cover the two defects that were reported:

1. An explicit creation request ("Add a task for RAM") was answered with a
   lookup of an existing task ("I couldn't find a matching task"). A
   creation never looks a task up, and an update never starts a new task.
2. A GET of a task that does not exist raised ``KeyError: 'task'``. Every
   answer now carries the same keys, also when there is no record.

The tests use the in-memory store of ``tests/test_task_intents.py``, so they
need neither PostgreSQL nor Cohere, and they drive the *frontend* routes
(``/tasks/chat`` and ``/tasks/{task_id}``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
from ai.text_extractor import (
    ExtractionResult,
    TaskQuery,
    TaskQueryFilters,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from tests.test_task_intents import FakeDatabase, sample_task


CREATE_MESSAGE = (
    "Add a task for RAM. The task is back-end optimization technique..."
)


# --- helpers ---------------------------------------------------------------------


class RecordingStore(FakeDatabase):
    """The in-memory store that remembers which repository call was used."""

    def __init__(self, tasks=None):
        super().__init__(tasks)
        self.calls: list[str] = []

    def get_task(self, task_id):
        self.calls.append("get_task")
        return super().get_task(task_id)

    def get_all_tasks(self):
        self.calls.append("get_all_tasks")
        return super().get_all_tasks()

    def search_tasks(self, **kwargs):
        self.calls.append("search_tasks")
        return super().search_tasks(**kwargs)

    def create_task(self, record):
        self.calls.append("create_task")
        return super().create_task(record)

    def run_sql_operation(self, operation, *, confirmation=None):
        self.calls.append("run_sql_operation")
        return super().run_sql_operation(operation, confirmation=confirmation)


class ScriptedCreateExtractor:
    """Answers the new-task extraction and refuses every other one."""

    def __init__(self, extractions):
        self.extractions = list(extractions)
        self.draft_calls: list[str] = []

    def extract_draft(self, user_input):
        self.draft_calls.append(user_input)

        if self.extractions:
            return self.extractions.pop(0)

        return ExtractionResult()

    def extract_update(self, user_input):
        raise AssertionError(
            "An explicit creation must not extract an update request."
        )

    def extract_query(self, user_input):
        raise AssertionError("An explicit creation must not look a task up.")


class ScriptedUpdateExtractor:
    """Answers the update extraction and refuses every other one."""

    def __init__(self, update):
        self.update = update
        self.update_calls: list[str] = []
        self.draft_calls: list[str] = []

    def extract_update(self, user_input):
        self.update_calls.append(user_input)
        return self.update

    def extract_draft(self, user_input):
        self.draft_calls.append(user_input)
        raise AssertionError(
            "An explicit update must not run the new-task extraction."
        )

    def extract_query(self, user_input):
        raise AssertionError("An explicit update must not look a task up.")


def create_extraction(**overrides) -> ExtractionResult:
    values = {
        "task_title": "Back-end optimization technique",
        "task_description": "A faster API response path",
        "assigned_to": "RAM",
        "assigned_when": "10 November 2026 at 10:30 AM",
        "deadline_when": "15 November 2026 at 5 PM",
        "reminder_when": None,
        "status": None,
        "progress": 50,
    }
    values.update(overrides)

    return ExtractionResult(**values)


# --- fixtures --------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def db(monkeypatch):
    """Install the in-memory database in the service layer."""

    store = RecordingStore()

    monkeypatch.setattr(task_service, "get_task", store.get_task)
    monkeypatch.setattr(task_service, "get_all_tasks", store.get_all_tasks)
    monkeypatch.setattr(task_service, "search_tasks", store.search_tasks)
    monkeypatch.setattr(task_service, "create_task", store.create_task)
    monkeypatch.setattr(task_service, "run_sql_operation", store.run_sql_operation)

    return store


# --- 1. an explicit creation is a creation ------------------------------------------


def test_create_message_starts_a_new_task(client, db, monkeypatch):
    extractor = ScriptedCreateExtractor(
        [
            create_extraction(),
            create_extraction(status="IN_PROGRESS"),
        ]
    )
    monkeypatch.setattr(task_service, "extractor", extractor)

    first = client.post("/tasks/chat", json={"message": CREATE_MESSAGE}).json()

    assert first["intent"] == "CREATE_TASK"
    assert first["stage"] == "confirm_fields"
    assert first["draft"]["task_title"] == "Back-end optimization technique"
    assert first["draft"]["task_description"] == "A faster API response path"
    assert first["draft"]["assigned_to"] == "RAM"
    # no task was looked up and nothing was written yet
    assert "search_tasks" not in db.calls
    assert db.inserts == 0

    second = client.post(
        "/tasks/chat",
        json={
            "message": "it is in progress",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["stage"] == "confirm_fields"
    assert second["draft"]["status"] == "IN_PROGRESS"

    reminder = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": second["stage"],
            "draft": second["draft"],
        },
    ).json()

    assert reminder["status"] == "reminder_required"
    assert reminder["stage"] == "reminder"

    final = client.post(
        "/tasks/chat",
        json={
            "message": "no reminder",
            "stage": reminder["stage"],
            "draft": reminder["draft"],
        },
    ).json()

    assert final["status"] == "success"
    assert final["intent"] == "CREATE_TASK"

    # exactly one record was inserted
    assert db.inserts == 1
    assert len(db.tasks) == 1
    assert db.updates == 0
    assert db.deletes == 0
    assert db.tasks[0]["task_title"] == "Back-end optimization technique"
    assert db.tasks[0]["task_description"] == "A faster API response path"
    assert db.tasks[0]["assigned_to"] == "RAM"
    assert db.tasks[0]["status"] == "IN_PROGRESS"


def test_create_message_does_not_look_a_task_up(client, db, monkeypatch):
    """The lookup of an existing task must never run for a creation."""

    db.tasks.append(sample_task(id=176, task_title="Business report"))
    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedCreateExtractor([create_extraction()]),
    )

    response = client.post("/tasks/chat", json={"message": CREATE_MESSAGE})

    assert response.status_code == 200
    body = response.json()

    assert body["intent"] == "CREATE_TASK"
    assert body["stage"] == "confirm_fields"
    assert "search_tasks" not in db.calls
    assert db.inserts == 0
    assert db.updates == 0


def test_an_explicit_create_leaves_a_pending_update_conversation(
    client, db, monkeypatch
):
    """A stale update stage must not swallow an explicit creation."""

    db.tasks.append(sample_task(id=176, task_title="Business report"))
    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedCreateExtractor([create_extraction()]),
    )

    response = client.post(
        "/tasks/chat",
        json={
            "message": CREATE_MESSAGE,
            "stage": "confirm_update",
            "draft": {
                "intent": "UPDATE_TASK",
                "task_id": None,
                "changes": {"progress": 70},
                "task": {},
                "find": {},
                "candidates": [],
            },
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["intent"] == "CREATE_TASK"
    assert body["stage"] == "confirm_fields"
    assert "search_tasks" not in db.calls
    assert db.updates == 0
    assert db.inserts == 0


# --- 2. an explicit update is an update --------------------------------------------


def test_an_explicit_update_leaves_a_pending_create_conversation(
    client, db, monkeypatch
):
    """A stale creation stage must not swallow an explicit update."""

    db.tasks.append(sample_task(id=176, task_title="Business report"))

    extractor = ScriptedUpdateExtractor(
        TaskUpdate(
            find=TaskUpdateFind(task_title="Business report"),
            changes=TaskUpdateChanges(progress=50),
        )
    )
    monkeypatch.setattr(task_service, "extractor", extractor)

    response = client.post(
        "/tasks/chat",
        json={
            "message": "Update my existing task progress to 50%",
            "stage": "fields",
            "draft": {
                "task_title": "Business report",
                "assigned_to": "Rahul",
                "assigned_date": None,
                "assigned_time": None,
                "deadline_date": None,
                "deadline_time": None,
                "status": None,
                "progress": None,
                "reminder_date": None,
                "reminder_time": None,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["intent"] == "UPDATE_TASK"
    assert body["status"] == "success"
    # the new-task extraction was never used for the update request
    assert extractor.draft_calls == []
    assert extractor.update_calls == ["Update my existing task progress to 50%"]
    assert db.updates == 1
    assert db.inserts == 0
    assert db.tasks[0]["progress"] == 50


def test_update_keeps_the_task_id_and_inserts_nothing(client, db, monkeypatch):
    db.tasks.append(sample_task(id=176, task_title="Business report"))

    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedUpdateExtractor(
            TaskUpdate(
                find=TaskUpdateFind(task_title="Business report"),
                changes=TaskUpdateChanges(progress=70),
            )
        ),
    )

    response = client.post(
        "/tasks/chat",
        json={"message": "Update my business report task progress to 70%."},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "success"
    assert body["intent"] == "UPDATE_TASK"
    assert body["task"]["id"] == 176

    assert db.inserts == 0
    assert len(db.tasks) == 1
    assert db.tasks[0]["id"] == 176
    assert db.tasks[0]["progress"] == 70
    # everything else survived the update
    assert db.tasks[0]["task_title"] == "Business report"
    assert db.tasks[0]["assigned_to"] == "Rahul"
    assert db.tasks[0]["status"] == "IN_PROGRESS"


def test_an_update_of_a_task_that_does_not_exist_does_not_insert(
    client, db, monkeypatch
):
    monkeypatch.setattr(
        task_service,
        "extractor",
        ScriptedUpdateExtractor(
            TaskUpdate(
                find=TaskUpdateFind(task_title="Ghost report"),
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
    assert db.inserts == 0
    assert db.updates == 0
    assert db.tasks == []


# --- 3. a read is consistent --------------------------------------------------------


def test_get_of_a_missing_task_returns_a_consistent_not_found(client, db):
    response = client.get("/tasks/999999")

    assert response.status_code == 404

    body = response.json()

    # the same keys as a successful read, so no client hits a KeyError
    assert body["status"] == "not_found"
    assert body["intent"] == "GET_TASK"
    assert body["task"] is None
    assert body["tasks"] == []
    assert body["message"]
    assert body["reply"] == body["message"]


def test_get_of_an_existing_task_returns_the_record(client, db):
    db.tasks.append(sample_task(id=176))

    response = client.get("/tasks/176")

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "success"
    assert body["task"]["id"] == 176
    assert body["task"]["task_title"] == "Business report"


def test_a_chat_read_of_a_missing_task_has_the_same_shape(client, db, monkeypatch):
    monkeypatch.setattr(
        task_service,
        "extractor",
        type(
            "QueryExtractor",
            (),
            {
                "extract_query": lambda self, text: TaskQuery(
                    find=TaskUpdateFind(task_title="Ghost report"),
                    filters=TaskQueryFilters(),
                ),
                "extract_update": lambda self, text: None,
            },
        )(),
    )

    response = client.post(
        "/tasks/chat",
        json={"message": "What is the status of my ghost report task?"},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "not_found"
    assert body["task"] is None
    assert body["tasks"] == []


# --- 4. the frontend and the backend agree on the port ------------------------------


def test_the_frontend_uses_the_documented_backend_port():
    """The frontend and the backend must talk to the same port.

    The frontend reads ``VITE_API_BASE_URL`` and falls back to the port the
    backend is documented to run on, so a test server on another port only
    needs the environment variable.
    """

    root = Path(__file__).resolve().parent.parent

    app_jsx = (root / "frontend" / "src" / "App.jsx").read_text()
    readme = (root / "README.md").read_text()

    assert "VITE_API_BASE_URL" in app_jsx
    assert '"http://localhost:8000"' in app_jsx
    assert "uvicorn app:app --reload --port 8000" in readme

