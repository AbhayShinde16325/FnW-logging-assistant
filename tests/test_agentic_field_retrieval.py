"""Agentic field retrieval and contextual follow-up tests.

These tests cover the behavior that used to fail: a follow-up such as
"What is its current deadline date?" must return the stored deadline, not a
generic task summary. The value must be read from the database on every call,
and the conversation's active task must only be used when the message points
back to it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, time

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
import services.query_rewriter as query_rewriter
from ai.text_extractor import (
    ExtractionResult,
    TaskQuery,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from database.connection import SessionLocal, create_tables
from database.models import Task
from services.task_agent import ReadRequest, TaskAgent


def _database_available() -> bool:
    try:
        create_tables()
    except Exception:
        return False

    session = SessionLocal()
    try:
        session.query(Task).count()
        return True
    finally:
        session.close()


DB_AVAILABLE = bool(os.getenv("DATABASE_URL")) and _database_available()


def sample_task(**overrides) -> dict:
    values = {
        "id": 1784,
        "task_title": "Business report",
        "task_description": "Monthly business report for the client",
        "assigned_to": "Rahul",
        "assigned_date": date(2026, 11, 10),
        "assigned_time": time(10, 30),
        "deadline_date": date(2026, 11, 21),
        "deadline_time": time(17, 0),
        "reminder_date": date(2026, 11, 20),
        "reminder_time": time(9, 0),
        "status": "IN_PROGRESS",
        "progress": 40,
    }
    values.update(overrides)
    return values


class StaticExtractor:
    def __init__(self, query=None, update=None, draft=None):
        self.query = query or TaskQuery()
        self.update = update or TaskUpdate()
        self.draft = draft or ExtractionResult()
        self.query_calls: list[str] = []
        self.update_calls: list[str] = []

    def extract_query(self, user_input):
        self.query_calls.append(user_input)
        return self.query

    def extract_update(self, user_input):
        self.update_calls.append(user_input)
        return self.update

    def extract_draft(self, user_input):
        return self.draft


# --- field answers ----------------------------------------------------------------


def test_a_deadline_follow_up_returns_the_deadline_not_a_summary(monkeypatch):
    task = sample_task()

    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(
        task_service, "get_task", lambda task_id: task if task_id == 1784 else None
    )

    result = task_service.process_get(
        "What is its current deadline date?",
        context_task_id=1784,
    )

    assert result["status"] == "success"
    assert result["requested_field"] == "deadline_date"
    assert result["reply"] == "21 November 2026"
    assert result["value"] == "21 November 2026"
    assert result["task"]["deadline_date"] == "2026-11-21"
    assert "is IN_PROGRESS at 40%" not in result["reply"]


def test_the_reminder_date_is_never_replaced_by_the_deadline(monkeypatch):
    task = sample_task(reminder_date=None, reminder_time=None)

    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: task)

    result = task_service.process_get(
        "What is its reminder date?",
        context_task_id=1784,
    )

    assert result["status"] == "success"
    assert result["requested_field"] == "reminder_date"
    assert result["reply"] == "No reminder date has been set."
    assert "21 November 2026" not in result["reply"]


@pytest.mark.parametrize(
    ("message", "field", "expected"),
    [
        ("What is the current progress?", "progress", "40%"),
        ("How much is completed?", "progress", "40%"),
        ("What is its status?", "status", "IN_PROGRESS"),
        ("Who is assigned to it?", "assigned_to", "Rahul"),
        ("What is its title?", "task_title", "Business report"),
        (
            "What is its expected outcome?",
            "task_description",
            "Monthly business report for the client",
        ),
    ],
)
def test_each_requested_field_is_answered_directly(
    monkeypatch, message, field, expected
):
    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: sample_task())

    result = task_service.process_get(message, context_task_id=1784)

    assert result["status"] == "success"
    assert result["requested_field"] == field
    assert result["reply"] == expected


def test_a_field_is_read_fresh_from_the_database_every_time(monkeypatch):
    task = sample_task(progress=30)

    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: task)

    first = task_service.process_get(
        "What is its current progress?", context_task_id=1784
    )

    task["progress"] = 75

    second = task_service.process_get(
        "What is its current progress?", context_task_id=1784
    )

    assert first["reply"] == "30%"
    assert second["reply"] == "75%"


def test_a_full_details_request_still_returns_the_summary(monkeypatch):
    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: sample_task())

    result = task_service.process_get(
        "Show me all the details of this task",
        context_task_id=1784,
    )

    assert result["status"] == "success"
    assert "Business report" in result["reply"]


# --- context resolution -----------------------------------------------------------


def test_a_contextual_follow_up_through_process_task(monkeypatch):
    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: sample_task())

    result = task_service.process_task(
        "What is its current status?",
        context_task_id=1784,
    )

    assert result["status"] == "success"
    assert result["reply"] == "IN_PROGRESS"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("How much is completed?", "40%"),
        ("When should I be reminded?", "20 November 2026"),
        ("Who is responsible?", "Rahul"),
        ("When is it due?", "21 November 2026"),
    ],
)
def test_natural_field_questions_reach_the_read_flow(monkeypatch, message, expected):
    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(task_service, "get_task", lambda task_id: sample_task())

    result = task_service.process_task(message, context_task_id=1784)

    assert result["status"] == "success"
    assert result["reply"] == expected


def test_an_explicit_task_id_wins_over_the_active_task(monkeypatch):
    task = sample_task(id=2000, task_title="Database review")
    query = TaskQuery(
        find=TaskUpdateFind(task_id=2000),
        requested_field="deadline_date",
    )

    monkeypatch.setattr(task_service, "extractor", StaticExtractor(query=query))
    monkeypatch.setattr(
        task_service,
        "get_task",
        lambda task_id: task if task_id == 2000 else None,
    )

    result = task_service.process_get(
        "What is the deadline of task 2000?",
        context_task_id=1784,
    )

    assert result["status"] == "success"
    assert result["task"]["id"] == 2000
    assert result["reply"] == "21 November 2026"


def test_an_ambiguous_field_question_without_context_never_guesses(monkeypatch):
    first = sample_task(id=1, task_title="Business report")
    second = sample_task(id=2, task_title="Database review")

    monkeypatch.setattr(task_service, "extractor", StaticExtractor())
    monkeypatch.setattr(
        task_service, "get_all_tasks", lambda: [first, second]
    )

    result = task_service.process_task("What is its deadline?")

    assert result["status"] != "success"
    assert result["status"] == "task_selection_required"


# --- a new task during a pending selection -----------------------------------------


def test_a_create_description_with_field_values_is_a_creation():
    message = (
        "At a new task, MCP server research for Ramesh, its deadline date and "
        "time are 20 November 3pm and its assigned date was 14 November 3pm. "
        "Its progress is 80% and status is in pending."
    )

    from ai.intent_detection import detect_intent

    assert detect_intent(message).name == "CREATE_TASK"
    assert detect_intent(message, task_in_context=True).name == "CREATE_TASK"

    # A declarative progress statement is not a field question.
    assert detect_intent("Its progress is 80%").name != "GET_TASK"

    # Real field follow-ups still reach the read flow.
    assert detect_intent("And its status?").name == "GET_TASK"
    assert detect_intent("What is its current deadline date?").name == "GET_TASK"


def test_a_new_task_escapes_a_pending_read_selection(monkeypatch):
    message = (
        "At a new task, MCP server research for Ramesh, its deadline date and "
        "time are 20 November 3pm and its assigned date was 14 November 3pm. "
        "Its progress is 80% and status is in pending."
    )

    extraction = ExtractionResult(
        task_title="MCP server research",
        assigned_to="Ramesh",
        assigned_when="14 November 2026 at 3 PM",
        deadline_when="20 November 2026 at 3 PM",
        reminder_when=None,
        status="PENDING",
        progress=80,
    )

    monkeypatch.setattr(
        task_service, "extractor", StaticExtractor(draft=extraction)
    )

    result = task_service.process_task(
        message,
        stage="select_task",
        draft={
            "intent": "GET_TASK",
            "action": "read",
            "candidates": [
                {"id": 2166, "task_title": "learning about HTML and CSS"},
                {"id": 2167, "task_title": "html and css learning"},
            ],
        },
    )

    assert result["intent"] == "CREATE_TASK"
    assert result["status"] != "task_selection_required"
    assert "which task to read" not in result.get("reply", "")
    assert result["draft"]["task_title"] == "MCP server research"
    assert result["draft"]["assigned_to"] == "Ramesh"
    assert result["draft"]["deadline_date"] == "2026-11-20"
    assert result["draft"]["deadline_time"] == "15:00"
    assert result["draft"]["progress"] == 80

    # Even a creation phrase that is not "create ..." leaves the selection.
    monkeypatch.setattr(task_service, "extractor", StaticExtractor())

    result = task_service.process_task(
        "i need to add this task to existing tasks",
        stage="select_task",
        draft={
            "intent": "GET_TASK",
            "action": "read",
            "candidates": [{"id": 2166, "task_title": "learning about HTML"}],
        },
    )

    assert result["intent"] == "CREATE_TASK"
    assert result["status"] != "task_selection_required"


# --- the agent loop ---------------------------------------------------------------


def test_the_agent_uses_the_context_tool_then_the_field_tool():
    calls = []

    def context_fn():
        calls.append("context")
        return {"active_task_id": 1784}

    def field_fn(task_id, field, employee=None):
        calls.append(("field", task_id, field))
        return {
            "status": "success",
            "task_id": task_id,
            "field": field,
            "columns": ["status"],
            "value": "IN_PROGRESS",
        }

    result = TaskAgent().run(
        ReadRequest(
            message="What is its status?",
            requested_field="status",
            use_context=True,
            context_task_id=1784,
            context_fn=context_fn,
            field_fn=field_fn,
        )
    )

    assert result["status"] == "success"
    assert result["value"] == "IN_PROGRESS"
    assert calls == ["context", ("field", 1784, "status")]


# --- the stored conversation (PostgreSQL) ------------------------------------------


class FakeText:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeMessage:
    def __init__(self, text: str):
        self.content = [FakeText(text)]


class FakeResponse:
    def __init__(self, text: str):
        self.message = FakeMessage(text)


class FailingRewriteClient:
    def chat(self, **kwargs):
        raise RuntimeError("the rewriter is down")


class IdQueryExtractor:
    def extract_query(self, user_input):
        match = re.search(r"task\s*(?:ID|id|#)?\s*(\d+)", user_input)

        find = TaskUpdateFind(
            task_id=int(match.group(1)) if match else None
        )

        return TaskQuery(find=find)

    def extract_update(self, user_input):
        return TaskUpdate(
            find=TaskUpdateFind(),
            changes=TaskUpdateChanges(),
        )


def insert_task(**overrides) -> int:
    values = sample_task(**overrides)
    values.pop("id")

    session = SessionLocal()
    try:
        task = Task(**values)
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id
    finally:
        session.close()


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
def test_the_acceptance_follow_ups_use_memory_and_fresh_rows(monkeypatch):
    marker = uuid.uuid4().hex[:8]
    task_id = insert_task(
        task_title=f"Business report {marker}",
        deadline_date=date(2026, 11, 21),
        reminder_date=date(2026, 11, 20),
        status="PENDING",
        progress=40,
    )

    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    monkeypatch.setattr(task_service, "extractor", IdQueryExtractor())
    monkeypatch.setattr(
        query_rewriter,
        "_resolve_client",
        lambda client=None: FailingRewriteClient(),
    )

    user = f"user-{uuid.uuid4().hex[:8]}"
    conversation = f"conversation-{uuid.uuid4().hex[:8]}"

    with TestClient(app_module.app) as client:
        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Get task {task_id}",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "success"
        assert first["task"]["id"] == task_id

        expected = [
            ("What is its current deadline date?", "21 November 2026"),
            ("What is its reminder date?", "20 November 2026"),
            ("What is its current progress?", "40%"),
            ("What is its status?", "PENDING"),
        ]

        for message, answer in expected:
            response = client.post(
                "/tasks/chat",
                json={
                    "message": message,
                    "conversation_id": conversation,
                    "user_id": user,
                },
            ).json()

            assert response["status"] == "success"
            assert response["reply"] == answer
            assert response["task"]["id"] == task_id