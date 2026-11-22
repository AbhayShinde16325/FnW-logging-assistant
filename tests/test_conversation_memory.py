"""Tests for the conversation memory and the query rewriting.

The tests cover three layers:

* ``services/query_rewriter`` - a follow-up message is turned into a
  standalone instruction (no database, no Cohere: a fake client is used),
* ``services/conversation_memory`` - the memory is stored in PostgreSQL,
  survives a restart and is isolated per user and conversation,
* the chat route - a follow-up message updates the task of the conversation
  instead of asking which task is meant.

The database tests are skipped automatically when PostgreSQL is not reachable.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
import services.query_rewriter as query_rewriter
from ai.text_extractor import (
    ExtractionResult,
    TaskQuery,
    TaskQueryFilters,
    TaskUpdate,
    TaskUpdateChanges,
    TaskUpdateFind,
)
from database.connection import SessionLocal, create_tables
from database.models import ConversationMemory, Task
from services.conversation_memory import (
    MAX_HISTORY_ENTRIES,
    append_history,
    clear_conversation_memory,
    get_conversation_memory,
    get_recent_messages,
    save_conversation_memory,
    update_active_task,
)


# --- helpers ---------------------------------------------------------------------


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


class FakeText:
    """One text segment of a model answer."""

    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeMessage:
    def __init__(self, text: str):
        self.content = [FakeText(text)]


class FakeResponse:
    def __init__(self, text: str):
        self.message = FakeMessage(text)


class FakeCohere:
    """A model client that answers with prepared JSON."""

    def __init__(self, answers=None, error: Exception | None = None):
        self.answers = list(answers or [])
        self.error = error
        self.prompts: list[str] = []

    def chat(self, **kwargs):
        messages = kwargs.get("messages") or []
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)

        if self.error is not None:
            raise self.error

        answer = self.answers.pop(0) if self.answers else {}

        return FakeResponse(json.dumps(answer))


def rewrite_answer(text: str, *, clarification: bool = False) -> dict:
    return {"rewritten": text, "needs_clarification": clarification}


def memory_with_task(task_id: int, **snapshot) -> dict:
    """A memory as the service stores it."""

    context = {"active_task": {"id": task_id, **snapshot}}

    return {
        "id": 1,
        "conversation_id": "conversation-1",
        "user_id": "user-1",
        "active_task_id": task_id,
        "last_action": "UPDATE_TASK",
        "context": context,
        "updated_at": None,
    }


class ScriptedExtractor:
    """A deterministic extractor, so no Cohere call is needed."""

    def __init__(self, title: str | None = None):
        self.title = title
        self.update_calls: list[str] = []
        self.draft_calls: list[str] = []
        self.query_calls: list[str] = []

    def extract_update(self, user_input: str) -> TaskUpdate:
        self.update_calls.append(user_input)

        find = TaskUpdateFind()
        changes = TaskUpdateChanges()

        match = re.search(r"task\s*(?:ID|id|#)\s*(\d+)", user_input, re.I)
        if match:
            find.task_id = int(match.group(1))
        elif self.title and self.title.lower() in user_input.lower():
            find.task_title = self.title

        match = re.search(r"deadline[^.]*?\bto\s+(.+)$", user_input, re.I)
        if match:
            changes.deadline_when = match.group(1).strip().rstrip(".")

        match = re.search(r"(\d+)\s*%", user_input)
        if match:
            changes.progress = int(match.group(1))

        return TaskUpdate(find=find, changes=changes)

    def extract_query(self, user_input: str) -> TaskQuery:
        self.query_calls.append(user_input)

        find = TaskUpdateFind()

        match = re.search(r"task\s*(?:ID|id|#)\s*(\d+)", user_input, re.I)
        if match:
            find.task_id = int(match.group(1))
        elif self.title and self.title.lower() in user_input.lower():
            find.task_title = self.title

        return TaskQuery(find=find, filters=TaskQueryFilters())

    def extract_draft(self, user_input: str) -> ExtractionResult:
        self.draft_calls.append(user_input)
        return ExtractionResult()


def insert_task(title: str, *, assigned_to: str = "Rahul", **overrides) -> int:
    values = {
        "task_title": title,
        "assigned_to": assigned_to,
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


# --- the query rewriter (no database) ----------------------------------------------


def test_a_follow_up_is_rewritten_with_the_active_task():
    client = FakeCohere(
        [rewrite_answer("Update the deadline of task ID 211 to 21 November 2026.")]
    )

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211, task_title="Business report"),
        client=client,
    )

    assert rewritten == "Update the deadline of task ID 211 to 21 November 2026."
    assert "21 November 2026" in rewritten


def test_a_short_follow_up_keeps_the_percentage():
    client = FakeCohere(
        [rewrite_answer("Update the progress of task ID 211 to 25%.")]
    )

    rewritten = query_rewriter.rewrite_query(
        "No, change it to 25%.",
        memory=memory_with_task(211, task_title="Business report", progress=10),
        client=client,
    )

    assert rewritten == "Update the progress of task ID 211 to 25%."


def test_a_standalone_message_is_not_rewritten():
    client = FakeCohere([rewrite_answer("something else")])

    rewritten = query_rewriter.rewrite_query(
        "Update task 211 to 25%.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Update task 211 to 25%."
    # The model was not even asked.
    assert client.prompts == []


def test_a_message_without_an_active_task_is_not_rewritten():
    client = FakeCohere([rewrite_answer("something else")])

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=None,
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."
    assert client.prompts == []


def test_a_new_task_is_never_built_from_the_memory():
    client = FakeCohere([rewrite_answer("something else")])

    rewritten = query_rewriter.rewrite_query(
        "Create a task for the business report",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Create a task for the business report"
    assert client.prompts == []


def test_a_bare_answer_is_not_rewritten():
    client = FakeCohere([rewrite_answer("something else")])

    for message in ("50%", "yes", "IN_PROGRESS", "21 November 2026"):
        rewritten = query_rewriter.rewrite_query(
            message, memory=memory_with_task(211), client=client
        )

        assert rewritten == message

    assert client.prompts == []


def test_a_list_request_is_not_rewritten():
    client = FakeCohere([rewrite_answer("something else")])

    rewritten = query_rewriter.rewrite_query(
        "Show all my pending tasks",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Show all my pending tasks"
    assert client.prompts == []


def test_an_invented_task_id_is_rejected():
    client = FakeCohere(
        [rewrite_answer("Update the deadline of task ID 999 to 21 November 2026.")]
    )

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."


def test_a_dropped_value_is_rejected():
    client = FakeCohere([rewrite_answer("Update the deadline of task ID 211.")])

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."


def test_a_rewrite_without_the_task_is_rejected():
    client = FakeCohere(
        [rewrite_answer("Update the deadline of the task to 21 November 2026.")]
    )

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."


def test_a_model_failure_falls_back_to_the_original_message():
    client = FakeCohere(error=RuntimeError("the model is not available"))

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."


def test_a_clarification_answer_keeps_the_original_message():
    client = FakeCohere(
        [rewrite_answer(None, clarification=True)]
    )

    rewritten = query_rewriter.rewrite_query(
        "Also update its deadline to 21 November 2026.",
        memory=memory_with_task(211),
        client=client,
    )

    assert rewritten == "Also update its deadline to 21 November 2026."


def test_the_prompt_carries_the_memory_and_the_history():
    client = FakeCohere([rewrite_answer("Update the progress of task ID 211 to 25%.")])

    memory = memory_with_task(211, task_title="Business report", progress=10)
    memory["context"]["history"] = [
        {"role": "user", "text": "Task #211."},
        {"role": "assistant", "text": "Task #211 updated successfully."},
    ]

    query_rewriter.rewrite_query(
        "No, change it to 25%.", memory=memory, client=client
    )

    prompt = client.prompts[0]

    assert "Active task id: 211" in prompt
    assert "Business report" in prompt
    assert "Task #211 updated successfully." in prompt
    assert 'No, change it to 25%' in prompt


def test_the_prompt_carries_the_last_action_and_the_candidates():
    client = FakeCohere([rewrite_answer("Show task ID 352.")])

    memory = memory_with_task(211, task_title="Business report")
    memory["context"]["candidates"] = [
        {"id": 351, "task_title": "MCP research"},
        {"id": 352, "task_title": "MCP report"},
    ]
    memory["context"]["clarification"] = {
        "stage": "select_task",
        "action": "read",
    }

    rewritten = query_rewriter.rewrite_query(
        "show me that one", memory=memory, client=client
    )

    assert rewritten == "Show task ID 352."

    prompt = client.prompts[0]

    assert "Last successful action: UPDATE_TASK" in prompt
    assert "Displayed task candidates" in prompt
    assert "task ID 351" in prompt
    assert "task ID 352" in prompt


def test_the_rewrite_may_pick_a_displayed_candidate():
    client = FakeCohere([rewrite_answer("Show task ID 352.")])

    memory = memory_with_task(211)
    memory["context"]["candidates"] = [{"id": 352, "task_title": "MCP report"}]

    rewritten = query_rewriter.rewrite_query(
        "show me that one", memory=memory, client=client
    )

    # The displayed candidate is a valid, known target, not an invented id.
    assert rewritten == "Show task ID 352."


# --- the memory service (PostgreSQL) -----------------------------------------------


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
class TestConversationMemory:

    def test_the_memory_is_created_and_read_back(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        assert get_conversation_memory(user, conversation) is None

        saved = save_conversation_memory(
            user, conversation, active_task_id=7, last_action="UPDATE_TASK"
        )

        assert saved["active_task_id"] == 7
        assert saved["last_action"] == "UPDATE_TASK"

        stored = get_conversation_memory(user, conversation)

        assert stored["active_task_id"] == 7
        assert stored["conversation_id"] == conversation
        assert stored["user_id"] == user

    def test_saving_twice_keeps_one_row(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        save_conversation_memory(user, conversation, active_task_id=1)
        save_conversation_memory(user, conversation, active_task_id=2)

        session = SessionLocal()
        try:
            rows = (
                session.query(ConversationMemory)
                .filter(
                    ConversationMemory.user_id == user,
                    ConversationMemory.conversation_id == conversation,
                )
                .count()
            )
        finally:
            session.close()

        assert rows == 1
        assert get_conversation_memory(user, conversation)["active_task_id"] == 2

    def test_the_memory_survives_a_new_session(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        update_active_task(user, conversation, 42, task={"id": 42, "task_title": "X"})

        # A new session is opened: the memory is read from PostgreSQL again.
        session = SessionLocal()
        try:
            row = (
                session.query(ConversationMemory)
                .filter(
                    ConversationMemory.user_id == user,
                    ConversationMemory.conversation_id == conversation,
                )
                .first()
            )

            assert row is not None
            assert row.active_task_id == 42
        finally:
            session.close()

        assert get_conversation_memory(user, conversation)["active_task_id"] == 42

    def test_the_memory_is_isolated_per_user_and_conversation(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        other_user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        other_conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        update_active_task(user, conversation, 1, task={"id": 1})

        assert get_conversation_memory(other_user, conversation) is None
        assert get_conversation_memory(user, other_conversation) is None

    def test_an_unknown_task_is_not_stored(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        assert update_active_task(user, conversation, 987654321) is None
        assert get_conversation_memory(user, conversation) is None

    def test_a_task_of_another_employee_is_not_stored(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        task_id = insert_task(f"Business report {uuid.uuid4().hex[:8]}", assigned_to="Rahul")

        assert update_active_task(user, conversation, task_id, employee="Someone") is None
        assert get_conversation_memory(user, conversation) is None

    def test_the_history_is_bounded(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        for index in range(20):
            append_history(
                user, conversation, [{"role": "user", "text": f"message {index}"}]
            )

        history = get_conversation_memory(user, conversation)["context"]["history"]

        assert len(history) == 10
        assert history[0]["text"] == "message 10"
        assert history[-1]["text"] == "message 19"
        assert all(turn.get("at") for turn in history)

    def test_the_memory_can_be_cleared(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        update_active_task(user, conversation, 5, task={"id": 5})

        assert clear_conversation_memory(user, conversation) is True
        assert get_conversation_memory(user, conversation) is None
        assert clear_conversation_memory(user, conversation) is False


    def test_every_message_has_a_timestamp(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        append_history(
            user,
            conversation,
            [
                {"role": "user", "text": "hello"},
                {"role": "assistant", "text": "hi"},
            ],
        )

        history = get_conversation_memory(user, conversation)["context"]["history"]

        assert [turn["role"] for turn in history] == ["user", "assistant"]

        for turn in history:
            assert datetime.fromisoformat(turn["at"]) is not None

    def test_the_latest_ten_messages_are_returned_in_order(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        turns = [
            {
                "role": "user",
                "text": f"message {index}",
                "at": f"2026-11-13T10:{index:02d}:00",
            }
            for index in range(12)
        ]

        append_history(user, conversation, turns)

        recent = get_recent_messages(user, conversation, 10)

        assert len(recent) == 10
        assert [turn["text"] for turn in recent] == [
            f"message {index}" for index in range(2, 12)
        ]
        assert [turn["at"] for turn in recent] == sorted(
            turn["at"] for turn in recent
        )
        assert len(recent) <= MAX_HISTORY_ENTRIES

    def test_the_recent_messages_are_isolated(self):
        user = f"user-{uuid.uuid4().hex[:8]}"
        other_user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        other_conversation = f"conversation-{uuid.uuid4().hex[:8]}"

        append_history(user, conversation, [{"role": "user", "text": "mine"}])
        append_history(
            other_user, conversation, [{"role": "user", "text": "theirs"}]
        )
        append_history(
            user, other_conversation, [{"role": "user", "text": "elsewhere"}]
        )

        assert [turn["text"] for turn in get_recent_messages(user, conversation)] == [
            "mine"
        ]
        assert [
            turn["text"]
            for turn in get_recent_messages(other_user, conversation)
        ] == ["theirs"]
        assert [
            turn["text"]
            for turn in get_recent_messages(user, other_conversation)
        ] == ["elsewhere"]

# --- the chat route (PostgreSQL) ----------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)

    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def extractor(monkeypatch):
    fake = ScriptedExtractor()

    monkeypatch.setattr(task_service, "extractor", fake)

    return fake


@pytest.fixture()
def rewriter(monkeypatch):
    """The model of the query rewriter, with prepared answers."""

    fake = FakeCohere()

    monkeypatch.setattr(query_rewriter, "_resolve_client", lambda client=None: fake)

    return fake


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
class TestChatWithMemory:

    def test_the_active_task_is_stored_after_a_successful_update(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        response = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["task"]["id"] == task_id

        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] == task_id
        assert memory["last_action"] == "UPDATE_TASK"

    def test_a_follow_up_updates_the_task_of_the_conversation(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "success"

        # The follow-up does not name the task any more.
        rewriter.answers.append(
            rewrite_answer(
                f"Update the deadline of task ID {task_id} to 21 November 2026."
            )
        )

        second = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its deadline to 21 November 2026",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert second["status"] == "success"
        assert second["task"]["id"] == task_id
        assert second["task"]["deadline_date"] == "2026-11-21"

        # The rewritten instruction is the one the agent worked on.
        assert extractor.update_calls[-1] == (
            f"Update the deadline of task ID {task_id} to 21 November 2026."
        )

        stored = read_task(task_id)
        assert stored.deadline_date == date(2026, 11, 21)

    def test_another_task_replaces_the_active_task(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        first_marker = uuid.uuid4().hex[:8]
        second_marker = uuid.uuid4().hex[:8]

        first_id = insert_task(f"Business report {first_marker}")
        second_id = insert_task(f"Database review {second_marker}")

        extractor.title = f"business report {first_marker}"

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {first_marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        assert (
            get_conversation_memory(user, conversation)["active_task_id"] == first_id
        )

        # The employee selects the other task.
        extractor.title = f"database review {second_marker}"

        client.post(
            "/tasks/chat",
            json={
                "message": f"Now update the progress of the database review {second_marker} to 20%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        assert (
            get_conversation_memory(user, conversation)["active_task_id"] == second_id
        )

        # The next follow-up works on the task that was selected last.
        rewriter.answers.append(
            rewrite_answer(
                f"Update the deadline of task ID {second_id} to 21 November 2026."
            )
        )

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its deadline to 21 November 2026",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["task"]["id"] == second_id
        assert read_task(second_id).deadline_date == date(2026, 11, 21)
        assert read_task(first_id).deadline_date == date(2026, 11, 15)

    def test_a_new_conversation_does_not_reuse_the_active_task(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        other_conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        # The new conversation has no memory, so the message is not rewritten.
        answer = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its deadline to 21 November 2026",
                "conversation_id": other_conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] != "success"
        assert get_conversation_memory(user, other_conversation)["active_task_id"] is None

    def test_another_user_does_not_see_the_memory(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        other_user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        assert get_conversation_memory(other_user, conversation) is None

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its deadline to 21 November 2026",
                "conversation_id": conversation,
                "user_id": other_user,
            },
        ).json()

        assert answer["status"] != "success"

    def test_a_turn_of_a_running_conversation_is_not_rewritten(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        # The assistant asks for a confirmation. The answer of the employee
        # belongs to the draft of the conversation and is never rewritten.
        draft = {
            "intent": "UPDATE_TASK",
            "task_id": task_id,
            "changes": {"progress": 40},
            "task": {},
            "find": {},
            "candidates": [],
        }

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "yes",
                "stage": "confirm_update",
                "draft": draft,
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] == "success"
        assert read_task(task_id).progress == 40
        assert rewriter.prompts == []

    def test_a_failed_operation_does_not_change_the_memory(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        assert get_conversation_memory(user, conversation)["active_task_id"] == task_id

        # The rewrite names a task that does not exist, so it is rejected and
        # the operation fails.
        rewriter.answers.append(
            rewrite_answer("Update the progress of task ID 987654321 to 10%.")
        )

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its progress to 20%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] != "success"

        memory = get_conversation_memory(user, conversation)

        # The active task of the conversation did not change.
        assert memory["active_task_id"] == task_id
        assert read_task(task_id).progress == 10

    def test_a_task_selection_is_never_a_creation(
        self, client, extractor, rewriter
    ):
        """The answer to "which task do you mean?" never starts a new task."""

        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        # The employee names the task of the conversation.
        answer = client.post(
            "/tasks/chat",
            json={
                "message": f"Task #{task_id}",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["intent"] != "CREATE_TASK"
        assert answer["status"] == "clarification_required"
        assert answer["draft"]["task_id"] == task_id

    def test_a_pending_change_is_kept_and_completed(
        self, client, extractor, rewriter
    ):
        """The change the employee asked for survives the clarification."""

        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        # The title is not unique, so the assistant has to ask which task is
        # meant.
        extractor.title = "business report"
        task_id = insert_task(f"Business report {marker}")

        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "task_selection_required"

        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] is None
        assert memory["context"]["pending"]["changes"] == {"progress": 10}

        # The employee names the task: the pending change is applied to it.
        rewriter.answers.append(
            rewrite_answer(f"Update the progress of task ID {task_id} to 10%.")
        )

        second = client.post(
            "/tasks/chat",
            json={
                "message": f"Task #{task_id}",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert second["status"] == "success"
        assert second["task"]["id"] == task_id
        assert read_task(task_id).progress == 10

        # The pending change is applied, so it is not pending any more.
        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] == task_id
        assert memory["context"]["pending"] == {}

    def test_the_conversation_is_restored_after_a_restart(
        self, client, extractor, rewriter
    ):
        """The memory is in PostgreSQL: a new backend reads it again."""

        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        extractor.title = f"business report {marker}"
        task_id = insert_task(f"Business report {marker}")

        client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the business report {marker} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        )

        # The backend is restarted: the memory is read from the database
        # again, with a new session.
        session = SessionLocal()
        try:
            row = (
                session.query(ConversationMemory)
                .filter(
                    ConversationMemory.user_id == user,
                    ConversationMemory.conversation_id == conversation,
                )
                .first()
            )

            assert row is not None
            assert row.active_task_id == task_id
        finally:
            session.close()

        # The conversation continues with the same memory.
        rewriter.answers.append(
            rewrite_answer(
                f"Update the deadline of task ID {task_id} to 21 November 2026."
            )
        )

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "Also update its deadline to 21 November 2026",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] == "success"
        assert answer["task"]["id"] == task_id
        assert read_task(task_id).deadline_date == date(2026, 11, 21)

    def test_a_positional_selection_is_restored_after_a_restart(
        self, client, extractor, rewriter
    ):
        """The candidates are in PostgreSQL, so "the second one" still works."""

        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        title = f"Business report {marker}"
        first_id = insert_task(title)
        second_id = insert_task(title)

        extractor.title = title

        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the {title} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "task_selection_required"
        candidates = first["tasks"]
        assert len(candidates) >= 2
        target = candidates[1]

        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] is None
        assert {row["id"] for row in memory["context"]["candidates"]} == {
            first_id,
            second_id,
        }
        assert memory["context"]["clarification"]["stage"] == "select_task"
        assert memory["context"]["pending"]["changes"] == {"progress": 10}

        # A new backend reads the memory. The client sends no stage or draft.
        answer = client.post(
            "/tasks/chat",
            json={
                "message": "the second one",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] == "success"
        assert answer["task"]["id"] == target["id"]
        assert read_task(target["id"]).progress == 10

        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] == target["id"]
        assert memory["context"]["candidates"] == []
        assert memory["context"]["clarification"] == {}

    def test_the_previous_one_selects_the_last_candidate(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        title = f"Business report {marker}"
        insert_task(title)
        insert_task(title)

        extractor.title = title

        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the {title} to 15%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "task_selection_required"
        target = first["tasks"][-1]

        answer = client.post(
            "/tasks/chat",
            json={
                "message": "the previous one",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["status"] == "success"
        assert answer["task"]["id"] == target["id"]
        assert read_task(target["id"]).progress == 15

    def test_an_explicit_create_wins_over_a_stored_selection(
        self, client, extractor, rewriter
    ):
        user = f"user-{uuid.uuid4().hex[:8]}"
        conversation = f"conversation-{uuid.uuid4().hex[:8]}"
        marker = uuid.uuid4().hex[:8]

        title = f"Business report {marker}"
        insert_task(title)
        insert_task(title)

        extractor.title = title

        first = client.post(
            "/tasks/chat",
            json={
                "message": f"Update the progress of the {title} to 10%",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert first["status"] == "task_selection_required"

        # The employee starts a new task instead of answering the question.
        answer = client.post(
            "/tasks/chat",
            json={
                "message": f"Create a task for a new report {marker}",
                "conversation_id": conversation,
                "user_id": user,
            },
        ).json()

        assert answer["intent"] == "CREATE_TASK"

        memory = get_conversation_memory(user, conversation)

        assert memory["context"]["candidates"] == []
        assert memory["context"]["clarification"] == {}
        assert memory["context"]["pending"] == {}


def test_the_prompt_carries_the_latest_ten_messages():
    client = FakeCohere(
        [rewrite_answer("Update the progress of task ID 211 to 25%.")]
    )

    memory = memory_with_task(211)
    memory["context"]["history"] = [
        {
            "role": "user",
            "text": f"turn-{index:02d}",
            "at": f"2026-11-13T10:{index:02d}:00",
        }
        for index in range(12)
    ]

    query_rewriter.rewrite_query(
        "No, change it to 25%.", memory=memory, client=client
    )

    prompt = client.prompts[0]

    assert "turn-00" not in prompt
    assert "turn-01" not in prompt

    for index in range(2, 12):
        assert f"turn-{index:02d}" in prompt

    assert "2026-11-13T10:11:00" in prompt


def test_a_pending_confirmation_is_restored_after_a_restart(
    client, extractor, rewriter
):
    user = f"user-{uuid.uuid4().hex[:8]}"
    conversation = f"conversation-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]

    task_id = insert_task(f"Business report {marker}")

    # The task is named, but the change is still missing.
    first = client.post(
        "/tasks/chat",
        json={
            "message": f"Update task #{task_id}",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert first["stage"] == "confirm_update"

    memory = get_conversation_memory(user, conversation)

    assert memory["context"]["clarification"]["stage"] == "confirm_update"
    assert memory["context"]["clarification"]["task_id"] == task_id

    # A new backend reads the clarification from PostgreSQL.
    answer = client.post(
        "/tasks/chat",
        json={
            "message": "change it to 25%",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert answer["status"] == "success"
    assert answer["task"]["id"] == task_id
    assert read_task(task_id).progress == 25


def test_a_rewritten_read_query_reaches_the_existing_agent(
    client, extractor, rewriter
):
    """Recent 10 messages -> rewriter -> existing agent -> SQL -> memory."""

    user = f"user-{uuid.uuid4().hex[:8]}"
    conversation = f"conversation-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]

    title = f"LangChain research {marker}"
    task_id = insert_task(
        title, assigned_to="Rahul", assigned_date=date(2026, 11, 10)
    )

    extractor.title = title

    first = client.post(
        "/tasks/chat",
        json={
            "message": f"Update the progress of the {title} task to 10%",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert first["status"] == "success"

    # The follow-up does not name the task. The rewriter turns it into a
    # standalone query that the existing agent understands.
    rewriter.answers.append(
        rewrite_answer(f"Retrieve the assigned date of task ID {task_id}.")
    )

    second = client.post(
        "/tasks/chat",
        json={
            "message": "What is its assigned date?",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert second["status"] == "success"
    assert second["intent"] == "GET_TASK"
    assert second["task"]["id"] == task_id
    assert second["task"]["assigned_date"] == "2026-11-10"

    # The rewritten query is the one the existing agent received.
    assert extractor.query_calls[-1] == (
        f"Retrieve the assigned date of task ID {task_id}."
    )

    prompt = rewriter.prompts[-1]

    assert title in prompt
    assert "What is its assigned date?" in prompt

    # Both messages of both turns are in PostgreSQL, with timestamps.
    history = get_conversation_memory(user, conversation)["context"]["history"]

    assert [turn["role"] for turn in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert history[2]["text"] == "What is its assigned date?"
    assert all(turn.get("at") for turn in history)


class ProgressiveCreateExtractor:
    """Two replies: first missing the description, then filling it."""

    def __init__(self, title: str):
        self.title = title
        self.extractions = [
            ExtractionResult(
                task_title=title,
                assigned_to="Rahul",
                deadline_when="15 November 2026 at 5 PM",
                status=None,
                progress=0,
            ),
            ExtractionResult(
                task_description="A deployment checklist for the release"
            ),
        ]

    def extract_draft(self, user_input: str) -> ExtractionResult:
        return self.extractions.pop(0)

    def extract_update(self, user_input: str) -> TaskUpdate:
        raise AssertionError("extract_update was not expected.")


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
def test_a_pending_creation_survives_without_echoed_stage(
    client, rewriter, monkeypatch
):
    user = f"user-{uuid.uuid4().hex[:8]}"
    conversation = f"conversation-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]
    title = f"Release checklist {marker}"

    monkeypatch.setattr(
        task_service, "extractor", ProgressiveCreateExtractor(title)
    )

    first = client.post(
        "/tasks/chat",
        json={
            "message": f"Create a task called {title} for Rahul due tomorrow at 5 PM",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert first["status"] == "clarification_required"
    assert first["missing_fields"] == ["task_description"]

    memory = get_conversation_memory(user, conversation)
    assert memory["context"]["task_creation"]["stage"] == "fields"
    assert memory["context"]["task_creation"]["draft"]["task_title"] == title

    second = client.post(
        "/tasks/chat",
        json={
            "message": "The expected outcome is a deployment checklist.",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert second["status"] == "confirmation_required"
    assert second["stage"] == "confirm_fields"
    assert (
        second["draft"]["task_description"]
        == "A deployment checklist for the release"
    )

    memory = get_conversation_memory(user, conversation)
    assert memory["context"]["task_creation"]["stage"] == "confirm_fields"
    assert (
        memory["context"]["task_creation"]["draft"]["task_description"]
        == "A deployment checklist for the release"
    )

    reminder = client.post(
        "/tasks/chat",
        json={
            "message": "yes",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert reminder["status"] == "reminder_required"
    assert reminder["stage"] == "reminder"

    memory = get_conversation_memory(user, conversation)
    assert memory["context"]["task_creation"]["stage"] == "reminder"

    final = client.post(
        "/tasks/chat",
        json={
            "message": "no reminder",
            "conversation_id": conversation,
            "user_id": user,
        },
    ).json()

    assert final["status"] == "success"
    assert final["intent"] == "CREATE_TASK"
    assert final["task"]["task_description"] == (
        "A deployment checklist for the release"
    )

    session = SessionLocal()
    try:
        count = (
            session.query(Task)
            .filter(Task.task_title == title)
            .count()
        )
    finally:
        session.close()

    assert count == 1
    assert get_conversation_memory(user, conversation)["context"][
        "task_creation"
    ] == {}


class HallucinatingExtractor:
    """An extractor that invents a task id that is not in the message."""

    def __init__(self, title: str):
        self.title = title

    def extract_update(self, user_input: str) -> TaskUpdate:
        return TaskUpdate(
            find=TaskUpdateFind(task_id=3, task_title=self.title),
            changes=TaskUpdateChanges(progress=10),
        )


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
def test_an_invented_task_id_is_ignored(monkeypatch):
    marker = uuid.uuid4().hex[:8]
    title = f"LangChain research {marker}"
    task_id = insert_task(title, assigned_to="Rahul")

    monkeypatch.setattr(
        task_service, "extractor", HallucinatingExtractor(title)
    )

    result = task_service.process_update(
        f"Update the progress of the {title} task to 10%",
        employee="Rahul",
    )

    assert result["status"] == "success"
    assert result["task"]["id"] == task_id
    assert read_task(task_id).progress == 10
