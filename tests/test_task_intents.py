"""End-to-end tests for the intent driven task flows.

The database is replaced by an in-memory store that applies the *structured
operation* the service produced, so these tests check the whole chain:

intent detection -> extraction -> task identification -> operation building ->
validation -> execution -> serialisation.

No PostgreSQL or Cohere access is needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
from ai.text_extractor import (
    TaskQuery,
    TaskQueryFilters,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from database.sql_generator import validate_operation
from tests.test_query_session import FakeExtractor, full_extraction


# --- in-memory database ----------------------------------------------------------


class FakeDatabase:
    """A tiny stand-in for the PostgreSQL store.

    It understands the structured operations produced by
    ``database.sql_generator``, so a wrong task id, a wrong column or a
    missing ownership condition shows up as a failed test.
    """

    def __init__(self, tasks=None):
        self.tasks = [dict(task) for task in (tasks or [])]
        self.next_id = max([task["id"] for task in self.tasks] or [0]) + 1
        self.inserts = 0
        self.updates = 0
        self.deletes = 0
        self.operations = []

    # --- repository API -------------------------------------------------------

    def get_task(self, task_id):
        for task in self.tasks:
            if task["id"] == task_id:
                return dict(task)
        return None

    def get_all_tasks(self):
        return [dict(task) for task in self.tasks]

    def search_tasks(
        self,
        *,
        task_id=None,
        task_title=None,
        assigned_to=None,
        owner=None,
        query=None,
        status=None,
        limit=None,
    ):
        matches = []

        for task in self.tasks:
            if task_id is not None and task["id"] != task_id:
                continue
            if owner and (task["assigned_to"] or "").strip().lower() != owner.strip().lower():
                continue
            if task_title and task_title.strip().lower() not in (
                task["task_title"] or ""
            ).lower():
                continue
            if assigned_to and assigned_to.strip().lower() not in (
                task["assigned_to"] or ""
            ).lower():
                continue
            if status and (task["status"] or "").upper() != status.upper():
                continue
            if query and query.strip().lower() not in (
                (task["task_title"] or "") + " " + (task["assigned_to"] or "")
            ).lower():
                continue
            matches.append(dict(task))

        matches.reverse()

        if limit is not None:
            matches = matches[:limit]

        return matches

    def create_task(self, record):
        values = record.model_dump() if hasattr(record, "model_dump") else dict(record)
        values.pop("id", None)

        task = {"id": self.next_id}
        task.update(values)
        self.next_id += 1
        self.inserts += 1
        self.tasks.append(task)

        return dict(task)

    # --- SQL execution layer --------------------------------------------------

    def run_sql_operation(self, operation, *, confirmation=None):
        validate_operation(operation)
        self.operations.append(operation)

        kind = operation["operation"]

        if kind == "UPDATE":
            task_id = operation["task_id"]
            owner = operation.get("owner")

            for task in self.tasks:
                if task["id"] != task_id:
                    continue
                if owner is not None and task["assigned_to"] != owner:
                    continue

                task.update(operation["updates"])
                self.updates += 1
                return {"status": "success", "operation": "UPDATE", "rows": []}

            return {"status": "error", "message": "Task not found."}

        if kind == "DELETE":
            task_id = operation["task_id"]
            owner = operation.get("owner")

            for index, task in enumerate(self.tasks):
                if task["id"] != task_id:
                    continue
                if owner is not None and task["assigned_to"] != owner:
                    continue

                self.tasks.pop(index)
                self.deletes += 1
                return {"status": "success", "operation": "DELETE", "rows": []}

            return {"status": "error", "message": "Task not found."}

        if kind == "SELECT":
            params = operation.get("params") or {}
            rows = []

            for task in self.tasks:
                if "task_id" in params and task["id"] != params["task_id"]:
                    continue
                if "status" in params and task["status"] != params["status"]:
                    continue
                if "assigned_to" in params:
                    needle = params["assigned_to"].strip("%").lower()
                    if needle not in (task["assigned_to"] or "").lower():
                        continue
                if "title_contains" in params:
                    needle = params["title_contains"].strip("%").lower()
                    if needle not in (task["task_title"] or "").lower():
                        continue
                rows.append(dict(task))

            rows.sort(key=lambda row: row["id"])
            return {"status": "success", "operation": "SELECT", "rows": rows}

        return {"status": "error", "message": "Unsupported operation."}


def _resolved_deadline() -> str:
    """The date the resolver gives for "17 November" right now."""

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from datetime_resolver import resolve

    result = resolve(
        "17 November",
        now=datetime.now(ZoneInfo("Asia/Kolkata")),
        timezone="Asia/Kolkata",
    )

    return result.resolved_date.isoformat()


def sample_task(**overrides):
    task = {
        "id": 1,
        "task_title": "Business report",
        "assigned_to": "Rahul",
        "assigned_date": "2026-11-10",
        "assigned_time": "10:30",
        "deadline_date": "2026-11-15",
        "deadline_time": "17:00",
        "reminder_date": "2026-11-15",
        "reminder_time": "16:30",
        "status": "PENDING",
        "progress": 0,
    }
    task.update(overrides)
    return task


# --- fake extractor ---------------------------------------------------------------


class ScriptedExtractor(FakeExtractor):
    """An extractor that returns canned update / query structures."""

    def __init__(self, extraction=None, update=None, query=None):
        super().__init__(extraction or full_extraction())
        self.update = update
        self.query = query

    def extract_update(self, user_input):
        if self.update is None:
            raise AssertionError("extract_update was not expected.")

        if isinstance(self.update, list):
            if not self.update:
                raise AssertionError("extract_update ran out of scripts.")
            return self.update.pop(0)

        return self.update

    def extract_query(self, user_input):
        if self.query is None:
            raise AssertionError("extract_query was not expected.")

        if isinstance(self.query, list):
            if not self.query:
                raise AssertionError("extract_query ran out of scripts.")
            return self.query.pop(0)

        return self.query


def update_request(
    *,
    task_id=None,
    find_title=None,
    find_assignee=None,
    status=None,
    progress=None,
    task_title=None,
    assigned_to=None,
    deadline_when=None,
):
    return TaskUpdate(
        find=TaskUpdateFind(
            task_id=task_id,
            task_title=find_title,
            assigned_to=find_assignee,
        ),
        changes=TaskUpdateChanges(
            task_title=task_title,
            assigned_to=assigned_to,
            deadline_when=deadline_when,
            status=status,
            progress=progress,
        ),
    )


def query_request(
    *,
    task_id=None,
    find_title=None,
    find_assignee=None,
    status=None,
    title_contains=None,
):
    return TaskQuery(
        find=TaskUpdateFind(
            task_id=task_id,
            task_title=find_title,
            assigned_to=find_assignee,
        ),
        filters=TaskQueryFilters(
            status=status,
            assigned_to=None,
            title_contains=title_contains,
        ),
    )


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def db(monkeypatch):
    """Install the in-memory database in the service layer."""

    store = FakeDatabase()

    monkeypatch.setattr(task_service, "get_task", store.get_task)
    monkeypatch.setattr(task_service, "get_all_tasks", store.get_all_tasks)
    monkeypatch.setattr(task_service, "search_tasks", store.search_tasks)
    monkeypatch.setattr(task_service, "create_task", store.create_task)
    monkeypatch.setattr(task_service, "run_sql_operation", store.run_sql_operation)

    return store


def use_extractor(monkeypatch, **kwargs):
    extractor = ScriptedExtractor(**kwargs)
    monkeypatch.setattr(task_service, "extractor", extractor)
    return extractor


# --- 1. creating a task inserts exactly one record --------------------------------


def test_create_inserts_exactly_one_record(client, db, monkeypatch):
    use_extractor(monkeypatch)

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

    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "confirm_fields",
            "draft": draft,
            "confirm": True,
        },
    )
    draft = response.json()["draft"]

    response = client.post(
        "/tasks/text",
        json={
            "text": "Rahul has a Python API research task",
            "stage": "reminder",
            "draft": draft,
            "reminder_text": "30 minutes before the deadline",
        },
    )
    draft = response.json()["draft"]

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
    assert response.json()["status"] == "success"
    assert db.inserts == 1
    assert len(db.tasks) == 1
    assert db.updates == 0
    assert db.deletes == 0


# --- 2-4. updating status / progress / both ---------------------------------------


def test_update_status_modifies_the_existing_record(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report", status="IN_PROGRESS"),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Change the status of my business report task to IN_PROGRESS."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["intent"] == "UPDATE_TASK"
    assert db.inserts == 0
    assert db.tasks[0]["status"] == "IN_PROGRESS"
    assert body["task"]["status"] == "IN_PROGRESS"


def test_update_progress_modifies_the_existing_record(client, db, monkeypatch):
    db.tasks.append(sample_task(status="IN_PROGRESS"))
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task progress to 70%."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert db.inserts == 0
    assert db.tasks[0]["progress"] == 70
    assert body["task"]["progress"] == 70


def test_update_status_and_progress_together(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        update=update_request(
            find_title="Business report", status="IN_PROGRESS", progress=70
        ),
    )

    response = client.post(
        "/tasks/text",
        json={
            "text": (
                "Update my business report task status to IN_PROGRESS "
                "and progress to 70%."
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert db.inserts == 0
    assert db.tasks[0]["status"] == "IN_PROGRESS"
    assert db.tasks[0]["progress"] == 70


# --- 5. one field changed, everything else preserved -------------------------------


def test_update_one_field_preserves_the_other_fields(client, db, monkeypatch):
    original = sample_task()
    db.tasks.append(original)
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task progress to 70%."},
    )

    assert response.status_code == 200

    stored = db.tasks[0]
    assert stored["task_title"] == original["task_title"]
    assert stored["assigned_to"] == original["assigned_to"]
    assert stored["assigned_date"] == original["assigned_date"]
    assert stored["assigned_time"] == original["assigned_time"]
    assert stored["deadline_date"] == original["deadline_date"]
    assert stored["deadline_time"] == original["deadline_time"]
    assert stored["reminder_date"] == original["reminder_date"]
    assert stored["reminder_time"] == original["reminder_time"]
    assert stored["progress"] == 70
    # only the requested field (and the status it implies) was written
    assert db.updates == 1
    assert set(db.operations[0]["updates"]) <= {"status", "progress"}


# --- 6. an ambiguous update asks the employee to pick a task ------------------------


def test_ambiguous_update_asks_for_clarification(client, db, monkeypatch):
    db.tasks.append(sample_task(id=1))
    db.tasks.append(sample_task(id=2))
    use_extractor(
        monkeypatch,
        update=update_request(find_title="report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my report task progress to 70%."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "task_selection_required"
    assert body["intent"] == "CLARIFICATION_REQUIRED"
    assert body["stage"] == "select_task"
    assert len(body["tasks"]) == 2
    assert db.updates == 0
    assert db.inserts == 0


# --- 7. updating a task that does not exist ----------------------------------------


def test_update_of_a_missing_task_does_not_insert(client, db, monkeypatch):
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Ghost report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my ghost report task progress to 70%."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "success"
    assert db.inserts == 0
    assert db.updates == 0
    assert db.tasks == []


# --- 8. a create request never updates an existing task ------------------------------


def test_create_request_does_not_update_an_existing_task(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(monkeypatch)

    response = client.post(
        "/tasks/text",
        json={"text": "Create a task to prepare the business report."},
    )

    assert response.status_code == 200
    assert response.json()["intent"] == "CREATE_TASK"
    assert db.updates == 0
    assert db.tasks[0]["status"] == "PENDING"
    assert db.tasks[0]["progress"] == 0


# --- 9. a read never changes anything -------------------------------------------------


def test_get_request_never_changes_records(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        query=query_request(find_title="Business report"),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "What is the current status of my business report task?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["intent"] == "GET_TASK"
    assert body["task"]["status"] == "PENDING"
    assert db.updates == 0
    assert db.inserts == 0
    assert db.deletes == 0


def test_read_task_endpoint_returns_the_stored_task(client, db, monkeypatch):
    db.tasks.append(sample_task())

    response = client.get("/tasks/1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["task"]["task_title"] == "Business report"
    assert db.updates == 0


# --- 10. delete needs an explicit confirmation -----------------------------------------


def test_delete_requires_confirmation(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        query=query_request(task_id=1),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Delete my business report task."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmation_required"
    assert "CONFIRM DELETE" in body["reply"]
    assert db.deletes == 0
    assert len(db.tasks) == 1


def test_delete_executes_after_the_exact_confirmation(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        query=query_request(task_id=1),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "CONFIRM DELETE", "task_id": 1},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert db.deletes == 1
    assert db.tasks == []


def test_delete_selection_is_kept_for_the_next_turn(client, db, monkeypatch):
    """The candidate list has to survive until the employee confirms."""

    db.tasks.append(sample_task(id=1, task_title="Business report"))
    db.tasks.append(sample_task(id=2, task_title="Quarterly report"))
    use_extractor(
        monkeypatch,
        query=query_request(find_title="report"),
    )

    first = client.post(
        "/tasks/chat",
        json={"message": "Delete my report task."},
    ).json()

    assert first["status"] == "task_selection_required"
    assert first["stage"] == "confirm_delete"
    assert first["draft"]["candidates"]
    assert db.deletes == 0

    # The employee confirms, but there are two candidates: the confirmation
    # is kept while the task is picked.
    second = client.post(
        "/tasks/chat",
        json={
            "message": "CONFIRM DELETE",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "task_selection_required"
    assert second["draft"]["confirmed"] is True
    assert db.deletes == 0

    third = client.post(
        "/tasks/chat",
        json={
            "message": "task 2",
            "stage": second["stage"],
            "draft": second["draft"],
        },
    ).json()

    assert third["status"] == "success"
    assert db.deletes == 1
    assert [task["id"] for task in db.tasks] == [1]


# --- 11. employee ownership -------------------------------------------------------------


def test_update_of_another_employees_task_is_refused(client, db, monkeypatch):
    db.tasks.append(sample_task(assigned_to="Rahul"))
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={
            "text": "Update my business report task progress to 70%.",
            "employee": "Someone Else",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "success"
    assert db.updates == 0
    assert db.tasks[0]["progress"] == 0


def test_update_of_the_employees_own_task_is_allowed(client, db, monkeypatch):
    db.tasks.append(sample_task(assigned_to="Rahul"))
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report", progress=70),
    )

    response = client.post(
        "/tasks/text",
        json={
            "text": "Update my business report task progress to 70%.",
            "employee": "Rahul",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert db.tasks[0]["progress"] == 70
    # the ownership condition was part of the statement
    assert "assigned_to" in db.operations[0]["sql"]
    assert db.operations[0]["params"]["assigned_to"] == "Rahul"


# --- 12. SQL injection attempts ----------------------------------------------------------


def test_sql_injection_in_a_title_cannot_change_the_database(client, db, monkeypatch):
    injection = "'; DROP TABLE tasks; --"

    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        update=update_request(
            find_title="Business report",
            task_title=injection,
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Rename my business report task to something else."},
    )

    assert response.status_code == 200

    # The injected text is stored as a plain value: it is bound as a
    # parameter, so it can never reach the parser as SQL.
    assert db.tasks[0]["task_title"] == injection
    assert db.deletes == 0
    assert len(db.tasks) == 1

    operation = db.operations[0]
    assert injection not in operation["sql"]
    assert operation["params"]["task_title"] == injection
    assert operation["sql"].count("WHERE id = :task_id") == 1


def test_sql_injection_in_the_search_is_parameterised(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        update=update_request(
            find_title="Business report'; DROP TABLE tasks; --",
            progress=70,
        ),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task progress to 70%."},
    )

    assert response.status_code == 200
    # the injected text is a value, not SQL: nothing was dropped and the
    # statement is still the generated, parameterised one.
    assert len(db.tasks) == 1
    if db.operations:
        assert db.operations[0]["sql"].count("DROP") == 0


# --- 13. the conversational context survives several turns -------------------------------


def test_conversation_context_is_preserved_across_turns(client, db, monkeypatch):
    use_extractor(monkeypatch)

    first = client.post(
        "/tasks/chat",
        json={"message": "Rahul has a Python API research task"},
    ).json()

    assert first["stage"] == "fields"
    assert first["missing_fields"] == ["status"]
    draft = first["draft"]
    assert draft["task_title"] == "Python API research"

    second = client.post(
        "/tasks/chat",
        json={
            "message": "it is in progress",
            "stage": first["stage"],
            "draft": draft,
        },
    ).json()

    assert second["stage"] == "confirm_fields"
    assert second["draft"]["status"] == "IN_PROGRESS"
    assert second["draft"]["task_title"] == "Python API research"
    assert second["draft"]["progress"] == 50
    draft = second["draft"]

    third = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": second["stage"],
            "draft": draft,
        },
    ).json()

    assert third["stage"] == "reminder"
    draft = third["draft"]

    fourth = client.post(
        "/tasks/chat",
        json={
            "message": "30 minutes before the deadline",
            "stage": third["stage"],
            "draft": draft,
        },
    ).json()

    assert fourth["stage"] == "confirm_reminder"
    draft = fourth["draft"]

    final = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": fourth["stage"],
            "draft": draft,
        },
    ).json()

    assert final["status"] == "success"
    assert db.inserts == 1
    assert db.tasks[0]["status"] == "IN_PROGRESS"
    assert db.tasks[0]["reminder_date"].isoformat() == "2026-11-15"


def test_ambiguous_update_keeps_the_context_for_the_next_turn(client, db, monkeypatch):
    db.tasks.append(sample_task(id=1, task_title="Business report"))
    db.tasks.append(sample_task(id=2, task_title="Quarterly report"))

    use_extractor(
        monkeypatch,
        update=update_request(find_title="report", progress=70),
    )

    first = client.post(
        "/tasks/chat",
        json={"message": "Update my business report task progress to 70%."},
    ).json()

    assert first["status"] == "task_selection_required"
    assert first["stage"] == "select_task"
    assert first["draft"]["changes"]["progress"] == 70

    # the employee picks the task in the next turn
    second = client.post(
        "/tasks/chat",
        json={
            "message": "the first one",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "success"
    # "the first one" means the first candidate of the list
    assert second["task"]["id"] == first["tasks"][0]["id"]
    updated = next(
        task for task in db.tasks if task["id"] == second["task"]["id"]
    )
    assert updated["progress"] == 70
    assert sum(1 for task in db.tasks if task["progress"] == 70) == 1


# --- extra: the intent is reported to the caller ----------------------------------------


def test_a_generic_confirmation_never_deletes_a_task(client, db, monkeypatch):
    """The CREATE flow confirmation must not be a delete confirmation."""

    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        query=query_request(task_id=1),
    )

    response = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "stage": "confirm_delete",
            "draft": {"task_id": 1, "candidates": [], "changes": {}},
            "confirm": True,
        },
    ).json()

    assert response["status"] != "success"
    assert db.deletes == 0
    assert len(db.tasks) == 1


def test_the_detected_intent_is_reported(client, db, monkeypatch):
    use_extractor(
        monkeypatch,
        query=query_request(status="PENDING"),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Show all my pending tasks."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "LIST_TASKS"
    assert body["count"] == 0


# --- the pending confirmation state -------------------------------------------


def test_a_contradicting_update_waits_for_a_confirmation(client, db, monkeypatch):
    db.tasks.append(sample_task(status="IN_PROGRESS", progress=50))
    use_extractor(
        monkeypatch,
        update=update_request(
            find_title="Business report", status="COMPLETED", progress=40
        ),
    )

    first = client.post(
        "/tasks/text",
        json={
            "text": (
                "Update my business report task status to COMPLETED "
                "and progress to 40%."
            )
        },
    ).json()

    assert first["status"] == "confirmation_required"
    assert first["stage"] == "confirm_update"
    assert db.updates == 0
    assert db.tasks[0]["progress"] == 50

    second = client.post(
        "/tasks/text",
        json={
            "text": "yes",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "success"
    assert db.updates == 1
    assert db.tasks[0]["status"] == "COMPLETED"
    assert db.tasks[0]["progress"] == 100


def test_an_update_without_a_value_asks_what_to_change(client, db, monkeypatch):
    db.tasks.append(sample_task())
    use_extractor(
        monkeypatch,
        update=update_request(find_title="Business report"),
    )

    response = client.post(
        "/tasks/text",
        json={"text": "Update my business report task."},
    ).json()

    assert response["status"] == "clarification_required"
    assert response["stage"] == "confirm_update"
    assert db.updates == 0


def test_a_pending_update_finds_its_task_in_the_next_turn(client, db, monkeypatch):
    """'I want to update a task' followed by the value must still work."""

    db.tasks.append(sample_task())

    use_extractor(
        monkeypatch,
        update=[
            # first turn: the employee did not say what to change
            update_request(find_title="Business report"),
            # second turn: only the new deadline
            update_request(find_title=None, deadline_when="17 November"),
        ],
    )

    first = client.post(
        "/tasks/chat",
        json={"message": "now i want to update task"},
    ).json()

    assert first["status"] == "clarification_required"
    assert first["stage"] == "confirm_update"

    second = client.post(
        "/tasks/chat",
        json={
            "message": (
                "Change the deadline to 17 November, that's it and nothing "
                "else to change."
            ),
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "success"
    assert second["intent"] == "UPDATE_TASK"
    assert db.updates == 1
    assert db.tasks[0]["deadline_date"].isoformat() == _resolved_deadline()
    # nothing else was touched
    assert db.tasks[0]["task_title"] == "Business report"
    assert db.tasks[0]["status"] == "PENDING"
    assert db.tasks[0]["progress"] == 0


def test_a_pending_update_asks_which_task_when_there_are_several(
    client, db, monkeypatch
):
    db.tasks.append(sample_task(id=1, task_title="Business report"))
    db.tasks.append(sample_task(id=2, task_title="Quarterly report"))

    use_extractor(
        monkeypatch,
        update=[
            update_request(find_title=None),
            update_request(find_title=None, deadline_when="17 November"),
        ],
    )

    first = client.post(
        "/tasks/chat",
        json={"message": "now i want to update task"},
    ).json()

    assert first["status"] == "clarification_required"

    second = client.post(
        "/tasks/chat",
        json={
            "message": "Change the deadline to 17 November.",
            "stage": first["stage"],
            "draft": first["draft"],
        },
    ).json()

    assert second["status"] == "task_selection_required"
    assert second["stage"] == "select_task"
    assert db.updates == 0
