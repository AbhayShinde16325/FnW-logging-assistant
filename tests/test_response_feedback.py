"""Tests for the thumbs up / thumbs down feedback on a conversation.

The feedback is stored on the existing ``conversation_memory`` row, so the
tests check the three states (TRUE, FALSE, NULL), changing a rating and the
fact that other memory updates never clear it.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

import app as app_module
from database.connection import SessionLocal, create_tables
from database.models import ConversationMemory, Task
from services.conversation_memory import (
    append_history,
    get_conversation_memory,
    save_conversation_memory,
    update_response_feedback,
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
    finally:
        session.close()


DB_AVAILABLE = bool(os.getenv("DATABASE_URL")) and _database_available()


def _ids() -> tuple[str, str]:
    return (
        f"user-{uuid.uuid4().hex[:8]}",
        f"conversation-{uuid.uuid4().hex[:8]}",
    )


def _stored_feedback(user: str, conversation: str):
    session = SessionLocal()

    try:
        return (
            session.query(ConversationMemory)
            .filter(
                ConversationMemory.user_id == user,
                ConversationMemory.conversation_id == conversation,
            )
            .one()
            .response_feedback
        )
    finally:
        session.close()


@pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not available.")
class TestResponseFeedback:

    def test_no_feedback_is_null(self):
        user, conversation = _ids()

        save_conversation_memory(user, conversation, last_action="GET_TASK")

        assert get_conversation_memory(user, conversation)["response_feedback"] is None

    def test_thumbs_up_stores_true(self):
        user, conversation = _ids()

        update_response_feedback(user, conversation, True)

        assert _stored_feedback(user, conversation) is True

    def test_thumbs_down_stores_false(self):
        user, conversation = _ids()

        update_response_feedback(user, conversation, False)

        assert _stored_feedback(user, conversation) is False

    def test_changing_the_rating_updates_the_same_row(self):
        user, conversation = _ids()

        update_response_feedback(user, conversation, True)
        update_response_feedback(user, conversation, False)

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
        assert _stored_feedback(user, conversation) is False

    def test_a_new_message_does_not_clear_the_feedback(self):
        user, conversation = _ids()

        save_conversation_memory(user, conversation, last_action="GET_TASK")
        update_response_feedback(user, conversation, True)

        append_history(
            user,
            conversation,
            [{"role": "assistant", "text": "A new answer."}],
        )
        save_conversation_memory(user, conversation, last_action="CREATE_TASK")

        assert _stored_feedback(user, conversation) is True

    def test_existing_memory_survives_the_rating(self):
        user, conversation = _ids()

        save_conversation_memory(
            user,
            conversation,
            active_task_id=7,
            last_action="UPDATE_TASK",
            context={"pending": {"progress": 25}},
        )

        update_response_feedback(user, conversation, False)

        memory = get_conversation_memory(user, conversation)

        assert memory["active_task_id"] == 7
        assert memory["last_action"] == "UPDATE_TASK"
        assert memory["context"]["pending"] == {"progress": 25}
        assert memory["response_feedback"] is False

    def test_the_feedback_endpoint_persists_the_rating(self):
        user, conversation = _ids()

        save_conversation_memory(user, conversation, last_action="GET_TASK")

        with TestClient(app_module.app) as client:
            up = client.post(
                "/tasks/feedback",
                json={
                    "conversation_id": conversation,
                    "user_id": user,
                    "feedback": True,
                },
            )
            down = client.post(
                "/tasks/feedback",
                json={
                    "conversation_id": conversation,
                    "user_id": user,
                    "feedback": False,
                },
            )

        assert up.status_code == 200
        assert up.json()["response_feedback"] is True
        assert down.status_code == 200
        assert down.json()["response_feedback"] is False
        assert _stored_feedback(user, conversation) is False
