from fastapi.testclient import TestClient
import app as app_module
import services.task_service as task_service
from ai.query_session import STAGE_CONFIRM_UPDATE, STAGE_CONFIRM_DELETE


class _Mem:
    def __init__(self): self.store = {}
    def load(self, user_id, employee, conversation_id):
        key = (user_id or employee, conversation_id)
        return self.store.get(key)
    def remember(self, **kwargs):
        key = (kwargs.get("user_id") or kwargs.get("employee"), kwargs.get("conversation_id"))
        ctx = self.store.get(key, {}).get("context", {})
        ctx = {**ctx}
        res = kwargs.get("result") or {}
        # mirror minimal behavior used by tests
        if res.get("followup", {}).get("asked"):
            ctx["post_execution_followup"] = {"eligible": True, "asked": True, "answered": False}
        elif res.get("status") == "cancelled":
            ctx["post_execution_followup"] = {}
        self.store[key] = {"context": ctx, "current_stage": res.get("stage"), "draft_payload": res.get("draft")}

def _client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    monkeypatch.setenv("AUTH_REQUIRED", "0")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setenv("DATABASE_AGENT", "0")
    mem = _Mem()
    monkeypatch.setattr(task_service, "_load_memory", mem.load)
    monkeypatch.setattr(task_service, "_remember_turn", mem.remember)
    monkeypatch.setattr(task_service, "save_conversation_memory", lambda identity, conv, **kw: mem.store.setdefault((identity, conv), {"context": {}})["context"].update(kw.get("context") or {}))
    monkeypatch.setattr(task_service, "append_history", lambda *a, **k: None)
    client = TestClient(app_module.app)
    client._mem = mem
    return client


def _task():
    return {"id": 49, "task_title": "Java Backend", "task_description": "d", "assigned_to": "Krishna", "assigned_date": "2026-11-12", "assigned_time": "10:00", "deadline_date": "2026-11-30", "deadline_time": "16:00", "status": "IN_PROGRESS", "progress": 67, "reminder_date": None, "reminder_time": None}


def test_successful_update_followed_by_one_nudge(monkeypatch):
    client = _client(monkeypatch)
    task = _task()
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 49 else None)
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [task])
    monkeypatch.setattr(task_service, "run_sql_operation", lambda op, confirmation=None: {"status": "success", "operation": "UPDATE", "rows": [task], "count": 1})
    draft = {"action": "update", "task_id": 49, "task_title": "Java Backend", "changes": {"progress": 67}, "candidates": []}
    res = client.post("/tasks/chat", json={"message": "CONFIRM UPDATE", "stage": STAGE_CONFIRM_UPDATE, "draft": draft, "confirm": True}).json()
    assert res["status"] == "success"
    assert res.get("followup", {}).get("asked") is True
    assert "anything else" in res["reply"].lower() or "what else" in res["reply"].lower()


def test_no_nudge_after_failed_operation(monkeypatch):
    client = _client(monkeypatch)
    task = _task()
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 49 else None)
    monkeypatch.setattr(task_service, "run_sql_operation", lambda op, confirmation=None: {"status": "error", "message": "db down"})
    draft = {"action": "update", "task_id": 49, "task_title": "Java Backend", "changes": {"progress": 67}, "candidates": []}
    res = client.post("/tasks/chat", json={"message": "CONFIRM UPDATE", "stage": STAGE_CONFIRM_UPDATE, "draft": draft, "confirm": True}).json()
    assert res["status"] != "success"
    assert "anything else" not in (res.get("reply") or "").lower()


def test_no_nudge_when_awaiting_confirmation(monkeypatch):
    client = _client(monkeypatch)
    task = _task()
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 49 else None)
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [task])
    res = client.post("/tasks/chat", json={"message": "Delete task 49"}).json()
    # should be confirmation required, no proactive success nudge
    assert res.get("status") in ("confirmation_required", "clarification_required")
    if res.get("status") == "confirmation_required":
        assert "deleted" not in (res.get("reply") or "").lower() or "CONFIRM" in (res.get("reply") or "")


def test_user_says_no_after_nudge(monkeypatch):
    client = _client(monkeypatch)
    task = _task()
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 49 else None)
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [task])
    monkeypatch.setattr(task_service, "run_sql_operation", lambda op, confirmation=None: {"status": "success", "operation": "UPDATE", "rows": [task], "count": 1})
    draft = {"action": "update", "task_id": 49, "task_title": "Java Backend", "changes": {"progress": 67}, "candidates": []}
    first = client.post("/tasks/chat", json={"message": "CONFIRM UPDATE", "stage": STAGE_CONFIRM_UPDATE, "draft": draft, "confirm": True, "conversation_id": "c1", "user_id": "u1"}).json()
    assert first.get("followup", {}).get("asked") is True
    second = client.post("/tasks/chat", json={"message": "No, nothing else", "conversation_id": "c1", "user_id": "u1"}).json()
    assert "let me know" in second["reply"].lower() or "alright" in second["reply"].lower()
    assert "anything else" not in second["reply"].lower().split("let me know")[0][-20:]


def test_user_says_yes_without_specifying(monkeypatch):
    client = _client(monkeypatch)
    task = _task()
    monkeypatch.setattr(task_service, "get_task", lambda tid: task if tid == 49 else None)
    monkeypatch.setattr(task_service, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(task_service, "search_tasks", lambda **kw: [task])
    monkeypatch.setattr(task_service, "run_sql_operation", lambda op, confirmation=None: {"status": "success", "operation": "UPDATE", "rows": [task], "count": 1})
    draft = {"action": "update", "task_id": 49, "task_title": "Java Backend", "changes": {"progress": 67}, "candidates": []}
    client.post("/tasks/chat", json={"message": "CONFIRM UPDATE", "stage": STAGE_CONFIRM_UPDATE, "draft": draft, "confirm": True, "conversation_id": "c2", "user_id": "u1"}).json()
    second = client.post("/tasks/chat", json={"message": "Yes", "conversation_id": "c2", "user_id": "u1"}).json()
    assert second["status"] in ("clarification_required", "success")
    assert "what would you like" in second["reply"].lower() or "help you with" in second["reply"].lower()
