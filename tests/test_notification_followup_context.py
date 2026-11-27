from types import SimpleNamespace

import services.task_service as ts


def test_notification_context_resolves_for_assigned_employee(monkeypatch):
    class Row:
        id = 10
        task_id = 1784
        notification_type = 'TASK_ASSIGNED'
        payload = {'kind':'task_assignment','has_missing_fields':True,'missing_fields':['deadline_date'],'followup_status':'pending','created_by_name':'Shreyansh Dixit'}
        is_read = False
    class Task:
        id = 1784
        task_title = 'Learn Java Backend'
        task_description = 'Learn'
        assigned_to_employee_id = 5
        deadline_date = None
        deadline_time = None
        progress = 0
        status = 'PENDING'
    class Q:
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def all(self): return [Row()]
        def first(self): return Task()
    class Session:
        def query(self, model):
            from database.models import Notification, Task as T
            if model is Notification:
                return Q()
            class TQ:
                def filter(self, *a, **k): return self
                def first(self): return Task()
            return TQ()
        def add(self, *a): pass
        def commit(self): pass
        def close(self): pass
    monkeypatch.setattr(ts, 'SessionLocal', lambda: Session())
    ctx = ts._notification_context_for_employee(5, 10)
    assert ctx['task_id'] == 1784
    assert ctx['created_by_name'] == 'Shreyansh Dixit'
    assert 'deadline_date' in ctx['missing_fields']


def test_notification_followup_handler_no_name_error(monkeypatch):
    """Regression: notification follow-up handler must not crash with NameError."""
    actor = SimpleNamespace(id=5, name="Krishna", email="k@example.com")
    task = SimpleNamespace(
        id=1784,
        task_title="Learn Java Backend",
        task_description="Learn",
        assigned_to_employee_id=5,
        assigned_to="Krishna",
        deadline_date=None,
        deadline_time=None,
        progress=0,
        status="PENDING",
    )

    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda task_id: task)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: ["deadline_date", "deadline_time"])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Deadline date", "Deadline time"])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)
    monkeypatch.setattr(ts, "get_task", lambda task_id: task)

    result = ts._handle_notification_followup_message(
        "Yes, I want to complete the missing task details.",
        nctx={
            "notification_id": 10,
            "task_id": 1784,
            "task_title": "Learn Java Backend",
            "created_by_name": "Shreyansh Dixit",
            "missing_fields": ["deadline_date", "deadline_time"],
            "missing_field_labels": ["Deadline date", "Deadline time"],
            "followup_status": "pending",
        },
        employee="Krishna",
        user_id="employee:5",
        conversation_id="conv-1",
    )

    assert result is not None
    assert result["status"] == "clarification_required"
    assert result["task_id"] == 1784
    assert "Learn Java Backend" in result["reply"]
    assert "What would you like to update or add to this task?" in result["reply"]


def test_missing_field_does_not_block_explicit_deadline_update(monkeypatch):
    actor = SimpleNamespace(id=5, name="Krishna", email="k@example.com")
    task = SimpleNamespace(
        id=87,
        task_title="OpenCode learning",
        task_description=None,
        assigned_to_employee_id=5,
        assigned_to="Krishna",
        deadline_date=None,
        deadline_time=None,
        reminder_date=None,
        reminder_time=None,
        progress=0,
        status="PENDING",
    )
    updated = SimpleNamespace(**{**task.__dict__, "deadline_date": __import__("datetime").date(2026, 11, 28), "deadline_time": __import__("datetime").time(17, 0)})

    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts, "_load_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_memory_identity", lambda *a, **k: "employee:5")
    monkeypatch.setattr(ts, "save_conversation_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_database_agent_enabled", lambda: False)
    monkeypatch.setattr(ts, "_post_followup_state", lambda *a, **k: {"eligible": False, "asked": False, "answered": False})
    monkeypatch.setattr(ts, "_notification_context_for_employee", lambda *a, **k: {
        "notification_id": 10, "task_id": 87, "task_title": "OpenCode learning",
        "created_by_name": "Mgr", "missing_fields": ["task_description"],
        "missing_field_labels": ["Task description"], "followup_status": "pending",
    })
    monkeypatch.setattr(ts, "get_task", lambda tid: task if tid == 87 else None)
    monkeypatch.setattr(ts, "get_all_tasks", lambda: [task])
    monkeypatch.setattr(ts, "search_tasks", lambda **k: [task])
    monkeypatch.setattr(ts.task_management, "update_task", lambda tid, updates: updated)
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)

    result = ts.process_chat_turn(
        "Update the deadline to tomorrow at 5 PM",
        stage="start",
        conversation_id="c1",
        user_id="u1",
        notification_id=10,
    )
    assert result["status"] != "clarification_required" or "Missing: Task description" not in (result.get("reply") or "")
    # Should be routed to update flow (may ask confirmation depending on existing rules)
    assert result.get("intent") in {ts.UPDATE_TASK, ts.CLARIFICATION_REQUIRED}


def test_notification_no_does_not_create_task_and_keeps_context(monkeypatch):
    actor = SimpleNamespace(id=5, name="Qwerty", email="q@example.com")
    task = SimpleNamespace(
        id=88, task_title="Learn about OpenClaw", task_description=None,
        assigned_to_employee_id=5, assigned_to="Qwerty",
        deadline_date=None, deadline_time=None, reminder_date=None, reminder_time=None,
        progress=0, status="PENDING",
    )
    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: ["task_description"])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Task description"])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)
    monkeypatch.setattr(ts, "get_task", lambda tid: task)
    created = []
    monkeypatch.setattr(ts, "create_task", lambda r: created.append(r) or task)

    result = ts._handle_notification_followup_message(
        "No",
        nctx={"notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw", "followup_status": "pending", "missing_fields": ["task_description"]},
        employee="Qwerty", user_id="employee:5", conversation_id="conv-1",
    )
    assert result["status"] == "clarification_required"
    assert "No problem" in result["reply"]
    assert "What else would you like to do with this task?" in result["reply"]
    assert created == []
    assert result["task_id"] == 88


def test_notification_followup_progress_update_after_no(monkeypatch):
    actor = SimpleNamespace(id=5, name="Qwerty", email="q@example.com")
    task = SimpleNamespace(
        id=88, task_title="Learn about OpenClaw", task_description=None,
        assigned_to_employee_id=5, assigned_to="Qwerty",
        deadline_date=None, deadline_time=None, reminder_date=None, reminder_time=None,
        progress=0, status="PENDING",
    )
    updated = SimpleNamespace(**{**task.__dict__, "progress": 40})
    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)
    monkeypatch.setattr(ts.task_management, "update_task", lambda tid, updates: updated)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: ["task_description"] if t is task else [])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Task description"])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)
    monkeypatch.setattr(ts, "get_task", lambda tid: task)

    result = ts._handle_notification_followup_message(
        "Update its progress to 40%",
        nctx={"notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw", "followup_status": "dismissed", "missing_fields": ["task_description"]},
        employee="Qwerty", user_id="employee:5", conversation_id="conv-1",
    )
    assert result["status"] == "success"
    assert result["task_id"] == 88
    assert "progress" in result["reply"].lower()


def test_notification_yes_with_stale_create_draft_does_not_enter_creation(monkeypatch):
    """Regression for observed bug: Yes on assignment notification with stale
    create-stage draft must open existing task, not task creation questionnaire."""
    actor = SimpleNamespace(id=5, name="Qwerty", email="q@example.com", has_permission=lambda p: True)
    task = SimpleNamespace(
        id=88, task_title="Learn about OpenClaw", task_description=None,
        assigned_to_employee_id=5, assigned_to="Qwerty",
        deadline_date=None, deadline_time=None, reminder_date=None, reminder_time=None,
        progress=0, status="PENDING",
    )
    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts, "_load_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_memory_identity", lambda *a, **k: "employee:5")
    monkeypatch.setattr(ts, "save_conversation_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_database_agent_enabled", lambda: False)
    monkeypatch.setattr(ts, "_post_followup_state", lambda *a, **k: {"eligible": False, "asked": False, "answered": False})
    monkeypatch.setattr(ts, "_notification_context_for_employee", lambda *a, **k: {
        "notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw",
        "created_by_name": "Mgr", "missing_fields": ["task_description"],
        "missing_field_labels": ["Task description"], "followup_status": "pending",
        "assignee_name": "Qwerty",
    })
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: ["task_description"])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Task description"])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)

    stale_create_draft = {
        "task_title": None, "task_description": None, "assigned_to": "Qwerty",
        "status": "PENDING", "progress": 0,
    }
    result = ts.process_chat_turn(
        "Yes, I want to complete the missing task details.",
        stage="fields",  # stale create stage
        draft=stale_create_draft,
        conversation_id="conv-1",
        user_id="u1",
        notification_id=10,
    )

    reply = result.get("reply") or result.get("message") or ""
    assert result.get("status") == "clarification_required"
    assert result.get("intent") == ts.UPDATE_TASK
    assert result.get("task_id") == 88
    assert "Learn about OpenClaw" in reply
    assert "What is the task title" not in reply
    assert "Tell me about the task" not in reply
    assert "check which details are missing" not in reply.lower()
    assert "What would you like to update or add to this task?" in reply
    # Must not be in task-creation flow
    assert result.get("stage") not in {"fields", "confirm_fields", "reminder", "confirm_reminder"}
    assert result.get("draft") is None
    assert result.get("notification_context", {}).get("task_id") == 88


def test_notification_no_preserves_context_and_allows_progress_update(monkeypatch):
    actor = SimpleNamespace(id=5, name="Qwerty", email="q@example.com", has_permission=lambda p: True)
    task = SimpleNamespace(
        id=88, task_title="Learn about OpenClaw", task_description=None,
        assigned_to_employee_id=5, assigned_to="Qwerty",
        deadline_date=None, deadline_time=None, reminder_date=None, reminder_time=None,
        progress=0, status="PENDING",
    )
    updated = SimpleNamespace(**{**task.__dict__, "progress": 40})
    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts, "_load_memory", lambda *a, **k: {"context": {"notification_followup": {
        "notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw",
        "created_by_name": "Mgr", "missing_fields": ["task_description"],
        "missing_field_labels": ["Task description"], "followup_status": "dismissed",
        "assignee_name": "Qwerty",
    }}})
    monkeypatch.setattr(ts, "_memory_identity", lambda *a, **k: "employee:5")
    monkeypatch.setattr(ts, "save_conversation_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_database_agent_enabled", lambda: False)
    monkeypatch.setattr(ts, "_post_followup_state", lambda *a, **k: {"eligible": False, "asked": False, "answered": False})
    monkeypatch.setattr(ts, "_notification_context_for_employee", lambda *a, **k: None)
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)
    monkeypatch.setattr(ts.task_management, "update_task", lambda tid, updates: updated)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: [] if getattr(t, "progress", 0) == 40 else ["task_description"])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Task description"] if fs else [])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)

    result = ts.process_chat_turn(
        "Update its progress to 40%",
        stage="start",
        conversation_id="conv-1",
        user_id="u1",
    )
    assert result.get("status") == "success"
    assert result.get("task_id") == 88
    assert "progress" in (result.get("reply") or "").lower()
    assert result.get("draft") is None or result.get("stage") not in {"fields", "confirm_fields", "reminder", "confirm_reminder"}


def test_explicit_new_task_request_still_creates_task(monkeypatch):
    actor = SimpleNamespace(id=5, name="Qwerty", email="q@example.com", has_permission=lambda p: True)
    task = SimpleNamespace(
        id=88, task_title="Learn about OpenClaw", task_description=None,
        assigned_to_employee_id=5, assigned_to="Qwerty",
        deadline_date=None, deadline_time=None, reminder_date=None, reminder_time=None,
        progress=0, status="PENDING",
    )
    monkeypatch.setattr(ts, "current_actor", lambda: actor)
    monkeypatch.setattr(ts, "_load_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_memory_identity", lambda *a, **k: "employee:5")
    monkeypatch.setattr(ts, "save_conversation_memory", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_database_agent_enabled", lambda: False)
    monkeypatch.setattr(ts, "_post_followup_state", lambda *a, **k: {"eligible": False, "asked": False, "answered": False})
    monkeypatch.setattr(ts, "_notification_context_for_employee", lambda *a, **k: {
        "notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw",
        "created_by_name": "Mgr", "missing_fields": ["task_description"],
        "missing_field_labels": ["Task description"], "followup_status": "pending",
        "assignee_name": "Qwerty",
    })
    monkeypatch.setattr(ts.task_management, "get_task_for_actor", lambda tid: task)
    monkeypatch.setattr(ts.missing_field_followup, "mark_followup", lambda *a, **k: None)
    monkeypatch.setattr(ts.missing_field_followup, "detect_missing_fields", lambda t: ["task_description"])
    monkeypatch.setattr(ts.missing_field_followup, "missing_field_labels", lambda fs: ["Task description"])
    monkeypatch.setattr(ts, "_remember_turn", lambda **k: None)

    # Explicit create request should NOT be handled by notification follow-up handler
    result = ts._handle_notification_followup_message(
        "Create a new task for preparing report",
        nctx={"notification_id": 10, "task_id": 88, "task_title": "Learn about OpenClaw", "followup_status": "pending", "missing_fields": ["task_description"]},
        employee="Qwerty", user_id="employee:5", conversation_id="conv-1",
    )
    assert result is None  # lets normal create flow handle it
