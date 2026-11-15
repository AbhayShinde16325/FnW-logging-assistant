"""Tests for ai/intent_detection.py.

The detector decides *what* the employee wants. A new task must never be
started by mistake, and an update request must never be mistaken for a
creation request.
"""

from __future__ import annotations

import pytest

from ai.intent_detection import (
    CLARIFICATION_REQUIRED,
    CREATE_TASK,
    DELETE_TASK,
    GENERAL_QUERY,
    GET_TASK,
    LIST_TASKS,
    UPDATE_TASK,
    detect_intent,
    is_explicit_create_request,
    is_explicit_update_request,
    is_task_reference_only,
)


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Create a task to prepare the business report.", CREATE_TASK),
        ("add a new task for the onboarding session", CREATE_TASK),
        ("Rahul has a Python API research task", CREATE_TASK),
        ("schedule a meeting with the team tomorrow", CREATE_TASK),
    ],
)
def test_create_requests(message, expected):
    assert detect_intent(message).name == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Update my business report task progress to 70%.", UPDATE_TASK),
        ("Change the status of my database task to IN_PROGRESS.", UPDATE_TASK),
        ("mark my report task as completed", UPDATE_TASK),
        ("I have completed the report task", UPDATE_TASK),
        ("rename my report task to monthly report", UPDATE_TASK),
    ],
)
def test_update_requests(message, expected):
    assert detect_intent(message).name == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        ("What is the current status of my database task?", GET_TASK),
        ("show me my database task", GET_TASK),
        ("check the progress of the business report task", GET_TASK),
    ],
)
def test_get_requests(message, expected):
    assert detect_intent(message).name == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Show all my pending tasks.", LIST_TASKS),
        ("show tasks with status pending", LIST_TASKS),
        ("what tasks are pending", LIST_TASKS),
        ("which tasks are in progress", LIST_TASKS),
        ("tasks that are pending", LIST_TASKS),
        ("get all tasks", LIST_TASKS),
        ("gather all pending tasks", LIST_TASKS),
        ("list all tasks", LIST_TASKS),
        ("give me an overview of my tasks", LIST_TASKS),
    ],
)
def test_list_requests(message, expected):
    assert detect_intent(message).name == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Delete task 5", DELETE_TASK),
        ("remove my business report task", DELETE_TASK),
        ("erase the database task", DELETE_TASK),
    ],
)
def test_delete_requests(message, expected):
    assert detect_intent(message).name == expected


@pytest.mark.parametrize(
    "message",
    [
        "hello",
        "thanks",
        "what can you do?",
        "asdfgh",
    ],
)
def test_general_messages(message):
    assert detect_intent(message).name == GENERAL_QUERY


def test_empty_message_needs_clarification():
    assert detect_intent("   ").name == CLARIFICATION_REQUIRED


def test_delete_wins_over_update_wording():
    # "change" appears in the sentence, but the employee wants a deletion.
    assert detect_intent("delete my task, change nothing").name == DELETE_TASK


def test_update_wins_over_create_wording():
    assert (
        detect_intent("Update my business report task progress to 70%").name
        == UPDATE_TASK
    )


def test_create_wins_when_the_employee_asks_for_a_new_task():
    assert (
        detect_intent("Create a task to update the documentation").name
        == CREATE_TASK
    )


def test_the_reason_is_reported():
    intent = detect_intent("Update my business report task progress to 70%.")

    assert intent.reason
    assert intent.confidence == 1.0
    assert intent.as_dict()["intent"] == UPDATE_TASK


# --- explicit creation requests -----------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Add a task for RAM. The task is back-end optimization technique...",
        "Add a task",
        "add a new task for the onboarding session",
        "Create a task",
        "Create a task for RAM.",
        "create a new task to prepare the business report",
        "I want to create a task for RAM",
        "please add a task for RAM",
    ],
)
def test_an_explicit_creation_is_always_a_creation(message):
    assert detect_intent(message).name == CREATE_TASK
    assert is_explicit_create_request(message) is True


def test_an_explicit_creation_is_never_a_lookup():
    # The wording of the task description may look like a question, the
    # request itself is still a creation.
    message = (
        "Add a task for RAM. The task is back-end optimization technique. "
        "Can you show me the status of it?"
    )

    assert detect_intent(message).name == CREATE_TASK


@pytest.mark.parametrize(
    "message",
    [
        "Update my existing task progress to 50%",
        "update my business report task",
        "change my business report task deadline",
        "rename my report task to monthly report",
    ],
)
def test_an_explicit_update_is_always_an_update(message):
    assert detect_intent(message).name == UPDATE_TASK
    assert is_explicit_update_request(message) is True


def test_a_pronoun_update_needs_an_identified_task():
    message = "Change its progress to 50%"

    # the conversation identifies the task
    assert detect_intent(message, task_in_context=True).name == UPDATE_TASK

    # the conversation does not know the task yet
    assert (
        detect_intent(message, task_in_context=False).name
        == CLARIFICATION_REQUIRED
    )

    # nothing is known about the conversation
    assert detect_intent(message).name == UPDATE_TASK


def test_a_creation_is_never_mistaken_for_an_update():
    assert (
        detect_intent("Create a task to update the documentation").name
        == CREATE_TASK
    )
    assert (
        detect_intent("Add a task to update the documentation").name
        == CREATE_TASK
    )


# --- a message that only names an existing task -------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Task #211",
        "task #211",
        "task 211",
        "#211",
        "the task 211.",
    ],
)
def test_a_task_reference_is_never_a_creation(message):
    """The answer to "which task do you mean?" is not a new task."""

    assert detect_intent(message).name == UPDATE_TASK
    assert is_task_reference_only(message) is True


def test_a_task_reference_is_not_a_task_description():
    assert detect_intent("Rahul has a Python API research task").name == (
        CREATE_TASK
    )
    assert is_task_reference_only("Rahul has a Python API research task") is False


def test_a_task_reference_with_an_action_is_still_an_update():
    assert detect_intent("Update task 211 progress to 50%").name == UPDATE_TASK
    assert detect_intent("Delete task 211").name == DELETE_TASK


def test_a_question_about_a_task_is_still_a_read():
    assert detect_intent("what is task 211").name == GET_TASK


@pytest.mark.parametrize(
    "message",
    [
        "Retrieve the assigned date of task ID 211.",
        "Fetch the deadline of task 211",
        "Get the progress of task 211",
    ],
)
def test_a_rewritten_read_query_is_a_get(message):
    """The rewriter's standalone phrasing reaches the read flow."""

    assert detect_intent(message).name == GET_TASK
