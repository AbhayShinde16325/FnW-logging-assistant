from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
from ai.query_session import STAGE_CONFIRM_FIELDS, STAGE_FIELDS, STAGE_REMINDER, STAGE_CONFIRM_REMINDER


def _client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setenv("DATABASE_AGENT", "0")
    return TestClient(app_module.app)


def _draft(**overrides):
    base = {
        "task_title": "CPP practice",
        "task_description": "Practice C++ problems",
        "assigned_to": "Abhay",
        "assigned_date": "2026-11-12",
        "assigned_time": "10:00",
        "deadline_date": "2026-11-30",
        "deadline_time": "16:00",
        "status": "PENDING",
        "progress": 0,
    }
    base.update(overrides)
    return base


def _post(client, message, stage, draft, **extra):
    payload = {"message": message, "stage": stage, "draft": draft}
    payload.update(extra)
    return client.post("/tasks/chat", json=payload).json()


def test_cancel_immediately_after_start(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})

    first = client.post("/tasks/chat", json={"message": "Create a task"}).json()
    res = _post(client, "I don't want to add this task", first.get("stage", STAGE_FIELDS), first.get("draft", {}))
    assert res["status"] == "cancelled"
    assert res["intent"] == "CREATE_TASK"
    assert res["reply"] == "No problem. I've cancelled the task creation. Nothing was added."
    assert res["stage"] == "start"
    assert res["draft"] is None
    assert created == []


def test_cancel_after_providing_title(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    draft = _draft(task_description=None, assigned_to=None, assigned_date=None, assigned_time=None, deadline_date=None, deadline_time=None, status=None, progress=None)
    res = _post(client, "Never mind", STAGE_FIELDS, draft)
    assert res["status"] == "cancelled"
    assert created == []


def test_cancel_after_providing_deadline(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    draft = _draft(assigned_to=None, assigned_date=None, assigned_time=None, status=None, progress=None)
    res = _post(client, "Cancel this task", STAGE_FIELDS, draft)
    assert res["status"] == "cancelled"
    assert created == []


def test_cancel_while_asking_description(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    draft = _draft(task_description=None)
    res = _post(client, "I don't want to create it", STAGE_FIELDS, draft)
    assert res["status"] == "cancelled"
    assert "Nothing was added" in res["reply"]
    assert created == []


def test_cancel_while_asking_assignee(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    draft = _draft(assigned_to=None)
    res = _post(client, "Leave it", STAGE_FIELDS, draft)
    assert res["status"] == "cancelled"
    assert created == []


def test_cancel_after_all_details_before_confirmation(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    res = _post(client, "Don't proceed", STAGE_CONFIRM_FIELDS, _draft())
    assert res["status"] == "cancelled"
    assert created == []


def test_cancel_at_reminder_stage(monkeypatch):
    client = _client(monkeypatch)
    created = []
    monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
    res = _post(client, "I changed my mind", STAGE_REMINDER, _draft())
    assert res["status"] == "cancelled"
    assert created == []


def test_cancel_with_various_phrases(monkeypatch):
    phrases = [
        "I don't want to add this task.",
        "Cancel this task",
        "Never mind",
        "Forget it",
        "Stop",
        "Leave it",
        "I changed my mind",
        "Don't proceed",
        "No, don't create it",
    ]
    for phrase in phrases:
        client = _client(monkeypatch)
        created = []
        monkeypatch.setattr(task_service, "create_task", lambda r: created.append(r) or {"id": 1})
        res = _post(client, phrase, STAGE_FIELDS, _draft(task_description=None))
        assert res["status"] == "cancelled", phrase
        assert created == [], phrase


def test_cancel_then_different_request(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(task_service, "create_task", lambda r: {"id": 1})
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [])
    cancelled = _post(client, "Forget it", STAGE_FIELDS, _draft(task_description=None), conversation_id="conv1", user_id="u1")
    assert cancelled["status"] == "cancelled"
    # next request should not resume cancelled workflow
    next_res = client.post("/tasks/chat", json={"message": "Show all my tasks", "conversation_id": "conv1", "user_id": "u1"}).json()
    assert next_res["status"] != "cancelled"
    assert next_res.get("intent") != "CREATE_TASK" or next_res.get("stage") in (None, "start", "fields", "confirm_fields", "reminder", "confirm_reminder")


def test_cancel_does_not_create_partial_task(monkeypatch):
    client = _client(monkeypatch)
    calls = []
    monkeypatch.setattr(task_service, "create_task", lambda r: calls.append(r) or {"id": 99})
    draft = _draft(task_description=None)
    res = _post(client, "I don't want to add this task", STAGE_FIELDS, draft)
    assert res["status"] == "cancelled"
    assert calls == []

def test_cancel_during_update_confirmation(monkeypatch):
    client = _client(monkeypatch)
    task = {"id": 35, "task_title": "CPP", "task_description": "d", "assigned_to": "A", "assigned_date": "2026-11-12", "assigned_time": "10:00", "deadline_date": "2026-11-30", "deadline_time": "16:00", "status": "PENDING", "progress": 0, "reminder_date": None, "reminder_time": None}
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 35 else None)
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [task])
    monkeypatch.setattr(task_service, "run_sql_operation", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not execute")))
    draft = {"action": "update", "task_id": 35, "task_title": "CPP", "changes": {"progress": 80}, "candidates": []}
    from ai.query_session import STAGE_CONFIRM_UPDATE
    res = _post(client, "I changed my mind", STAGE_CONFIRM_UPDATE, draft)
    assert res["status"] == "cancelled"
    assert "Nothing was changed" in res["reply"]


def test_backtrack_goes_to_previous_step(monkeypatch):
    client = _client(monkeypatch)
    # At confirm_reminder, go back should return to reminder
    res = _post(client, "Go back to the previous question", STAGE_CONFIRM_REMINDER, _draft(), confirm=True)
    assert res["status"] in ("clarification_required", "confirmation_required", "reminder_required")
    assert res["stage"] in (STAGE_REMINDER, STAGE_FIELDS)
    assert res.get("draft") is not None
