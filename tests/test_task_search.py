"""Regression tests for the natural-language task search.

The search has to understand what the employee means, not just compare the
whole sentence with one substring:

* "research about MCP" finds "Researching about MCP servers",
* "MCP servers task" finds it too,
* singular/plural, synonyms and spelling variants still find it,
* a free-text query also covers the assignee and the status,
* several matches are returned in relevance order,
* a lookup never returns another employee's task.

The database tests are skipped automatically when PostgreSQL is not
reachable. They only insert rows; nothing is ever deleted.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, time

import pytest
from sqlalchemy.exc import SQLAlchemyError

import services.task_service as task_service
from ai.intent_detection import (
    CREATE_TASK,
    GET_TASK,
    LIST_TASKS,
    UPDATE_TASK,
    detect_intent,
    is_search_request,
)
from ai.query_session import STAGE_SELECT_TASK
from ai.text_extractor import (
    TaskQuery,
    TaskQueryFilters,
    TaskUpdateFind,
)
from database.connection import SessionLocal, create_tables
from database.models import Task
from database.sql_generator import build_select_operation, validate_operation
from database.task_repository import search_tasks


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


DB_AVAILABLE = bool(os.getenv("DATABASE_URL")) and _database_available()

requires_database = pytest.mark.skipif(
    not DB_AVAILABLE, reason="PostgreSQL is not available."
)


def insert_task(**overrides) -> int:
    values = {
        "task_title": "Business report",
        "assigned_to": "Rahul",
        "status": "PENDING",
        "progress": 0,
        "assigned_date": date(2026, 11, 10),
        "assigned_time": time(10, 30),
        "deadline_date": date(2026, 11, 15),
        "deadline_time": time(17, 0),
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


def marker() -> str:
    return f"srch{uuid.uuid4().hex[:10]}"


class ScriptedQueryExtractor:
    """Returns a prepared TaskQuery instead of calling the model."""

    def __init__(self, query):
        self.query = query

    def extract_query(self, user_input):
        return self.query


# --- intent detection (no database) ------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "do I have a task like research about MCP",
        "research about MCP",
        "MCP servers task",
        "find my MCP research task",
        "look for the MCP server work",
    ],
)
def test_a_lookup_is_never_a_creation(message):
    assert detect_intent(message).name == GET_TASK
    assert is_search_request(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "find tasks about research",
        "search all tasks about MCP",
    ],
)
def test_a_plural_lookup_lists_tasks(message):
    assert detect_intent(message).name == LIST_TASKS


def test_an_action_verb_keeps_a_creation_a_creation():
    assert detect_intent("Create a task about MCP").name == CREATE_TASK
    assert detect_intent("Add a task for RAM").name == CREATE_TASK
    assert detect_intent("Rahul has a Python API research task").name == (
        CREATE_TASK
    )
    assert is_search_request("Rahul has a Python API research task") is False


def test_an_update_stays_an_update():
    assert detect_intent("Update my MCP task progress to 70%").name == UPDATE_TASK
    assert is_search_request("Update my MCP task progress to 70%") is False


# --- the SQL generator --------------------------------------------------------------


def test_the_select_operation_uses_several_terms():
    operation = build_select_operation(title_contains="research about MCP")

    assert "OR" in operation["sql"]
    assert operation["sql"].count("task_title ILIKE") >= 2
    assert validate_operation(operation) is operation

    # every term is a bound parameter, never inlined SQL
    assert operation["params"]
    assert all("research" not in sql for sql in [operation["sql"]])

    for value in operation["params"].values():
        assert value.startswith("%") and value.endswith("%")


def test_the_select_operation_has_an_exact_owner_scope():
    operation = build_select_operation(owner="Rahul")

    assert "assigned_to ILIKE :owner" in operation["sql"]
    assert operation["params"]["owner"] == "Rahul"
    assert validate_operation(operation) is operation


# --- the repository search ------------------------------------------------------------


@requires_database
def test_an_exact_title_is_found():
    tag = marker()
    title = f"Quarterly report {tag}"
    task_id = insert_task(task_title=title)

    rows = search_tasks(task_title=title)

    assert rows
    assert rows[0].id == task_id


@requires_database
def test_a_partial_title_is_found():
    tag = marker()
    task_id = insert_task(task_title=f"Quarterly report {tag}")

    rows = search_tasks(task_title=f"quarterly {tag}")

    assert any(row.id == task_id for row in rows)


@requires_database
def test_research_about_mcp_finds_researching_about_mcp_servers():
    tag = marker()
    task_id = insert_task(task_title=f"Researching about MCP servers {tag}")

    # The reported phrase, verbatim.
    rows = search_tasks(task_title="research about MCP")
    assert any(row.id == task_id for row in rows)

    # With the unique tag the exact task wins.
    rows = search_tasks(task_title=f"research about MCP {tag}")
    assert rows
    assert rows[0].id == task_id


@requires_database
def test_mcp_servers_task_finds_the_task():
    tag = marker()
    task_id = insert_task(task_title=f"Researching about MCP servers {tag}")

    rows = search_tasks(task_title=f"MCP servers task {tag}")

    assert rows
    assert rows[0].id == task_id


@requires_database
def test_a_spelling_mistake_still_finds_the_task():
    tag = marker()
    task_id = insert_task(task_title=f"Researching about MCP servers {tag}")

    rows = search_tasks(task_title=f"reserch about mcp {tag}")

    assert any(row.id == task_id for row in rows)


@requires_database
def test_a_synonym_still_finds_the_task():
    tag = marker()
    task_id = insert_task(task_title=f"Researching about MCP servers {tag}")

    rows = search_tasks(task_title=f"investigate MCP servers {tag}")

    assert any(row.id == task_id for row in rows)


@requires_database
def test_singular_and_plural_find_the_same_task():
    tag = marker()
    task_id = insert_task(task_title=f"Researching about MCP server {tag}")

    assert any(
        row.id == task_id
        for row in search_tasks(task_title=f"servers {tag}")
    )
    assert any(
        row.id == task_id
        for row in search_tasks(task_title=f"server {tag}")
    )


@requires_database
def test_a_status_word_is_used_as_a_filter():
    tag = marker()
    completed = insert_task(
        task_title=f"MCP servers {tag}", status="COMPLETED", progress=100
    )
    insert_task(task_title=f"MCP servers {tag}", status="PENDING", progress=0)

    rows = search_tasks(task_title=f"{tag} completed")

    assert [row.id for row in rows] == [completed]


@requires_database
def test_a_free_text_query_covers_the_assignee():
    tag = marker()
    assignee = f"Nisha {tag}"
    task_id = insert_task(
        task_title=f"Release checklist {tag}", assigned_to=assignee
    )

    rows = search_tasks(query=f"{tag} Nisha")

    assert any(row.id == task_id for row in rows)


@requires_database
def test_an_unrelated_query_returns_nothing():
    tag = marker()
    insert_task(task_title=f"Researching about MCP servers {tag}")

    assert search_tasks(task_title=f"quantum blockchain {tag}") == []


@requires_database
def test_several_matches_come_back_in_relevance_order():
    tag = marker()
    insert_task(task_title=f"Marketing plan {tag}")
    best = insert_task(task_title=f"Marketing plan MCP {tag}")

    rows = search_tasks(task_title=f"MCP marketing {tag}")

    assert rows
    assert rows[0].id == best


@requires_database
def test_the_owner_scope_hides_another_employees_task():
    tag = marker()
    mine = insert_task(
        task_title=f"MCP research {tag}", assigned_to=f"Rahul {tag}"
    )
    insert_task(
        task_title=f"MCP research {tag}", assigned_to=f"Priya {tag}"
    )

    rows = search_tasks(task_title=f"MCP research {tag}", owner=f"Rahul {tag}")

    assert [row.id for row in rows] == [mine]
    assert search_tasks(task_title=f"MCP research {tag}", owner="Nobody") == []


# --- the service / API flow ---------------------------------------------------------


@requires_database
def test_a_lookup_by_title_is_scoped_to_the_employee(monkeypatch):
    tag = marker()
    task_id = insert_task(
        task_title=f"Researching about MCP servers {tag}",
        assigned_to=f"Priya {tag}",
    )

    query = TaskQuery(
        find=TaskUpdateFind(task_title=f"research about MCP {tag}"),
        filters=TaskQueryFilters(),
    )
    monkeypatch.setattr(
        task_service, "extractor", ScriptedQueryExtractor(query)
    )

    # The owner sees the task.
    found = task_service.process_get(
        f"do I have a task like research about MCP {tag}",
        employee=f"Priya {tag}",
    )
    assert found["status"] == "success"
    assert found["task"]["id"] == task_id

    # Another employee does not, and no data leaks.
    hidden = task_service.process_get(
        f"do I have a task like research about MCP {tag}",
        employee=f"Rahul {tag}",
    )
    assert hidden["status"] == "not_found"
    assert hidden["task"] is None


@requires_database
def test_the_reported_lookup_finds_the_task_end_to_end(monkeypatch):
    tag = marker()
    task_id = insert_task(
        task_title=f"Researching about MCP servers {tag}",
        assigned_to=f"Rahul {tag}",
    )

    query = TaskQuery(
        find=TaskUpdateFind(task_title=f"research about MCP {tag}"),
        filters=TaskQueryFilters(),
    )
    monkeypatch.setattr(
        task_service, "extractor", ScriptedQueryExtractor(query)
    )

    result = task_service.process_task(
        f"do I have a task like research about MCP {tag}",
        employee=f"Rahul {tag}",
    )

    assert result["status"] == "success"
    assert result["intent"] == GET_TASK
    assert result["task"]["id"] == task_id


@requires_database
def test_a_read_selection_is_completed_on_the_next_turn(monkeypatch):
    tag = marker()
    first = insert_task(task_title=f"Business report {tag}")
    second = insert_task(task_title=f"Quarterly report {tag}")

    result = task_service.process_task(
        "the first one",
        stage=STAGE_SELECT_TASK,
        draft={
            "intent": GET_TASK,
            "action": "read",
            "candidates": [
                {"id": first, "task_title": f"Business report {tag}"},
                {"id": second, "task_title": f"Quarterly report {tag}"},
            ],
            "changes": {},
        },
        employee="Rahul",
    )

    assert result["status"] == "success"
    assert result["task"]["id"] == first


@requires_database
def test_the_list_flow_ranks_multi_word_searches(monkeypatch):
    tag = marker()
    best = insert_task(
        task_title=f"Researching about MCP servers {tag}", assigned_to="Rahul"
    )
    insert_task(task_title=f"Researching about testing {tag}", assigned_to="Rahul")

    query = TaskQuery(
        find=TaskUpdateFind(),
        filters=TaskQueryFilters(title_contains=f"research MCP {tag}"),
    )
    monkeypatch.setattr(
        task_service, "extractor", ScriptedQueryExtractor(query)
    )

    result = task_service.process_list(
        f"show me tasks about research MCP {tag}", employee="Rahul"
    )

    assert result["status"] == "success"
    assert result["tasks"]
    assert result["tasks"][0]["id"] == best


@requires_database
def test_the_list_flow_never_shows_another_employees_tasks(monkeypatch):
    tag = marker()
    mine = insert_task(
        task_title=f"MCP research {tag}", assigned_to=f"Priya {tag}"
    )
    insert_task(
        task_title=f"MCP research {tag}", assigned_to=f"Rahul {tag}"
    )

    query = TaskQuery(
        find=TaskUpdateFind(),
        filters=TaskQueryFilters(title_contains=f"MCP research {tag}"),
    )
    monkeypatch.setattr(
        task_service, "extractor", ScriptedQueryExtractor(query)
    )

    result = task_service.process_list(
        f"list my tasks about MCP research {tag}", employee=f"Priya {tag}"
    )

    assert result["status"] == "success"
    assert [task["id"] for task in result["tasks"]] == [mine]


@requires_database
def test_listing_another_employee_by_name_returns_nothing(monkeypatch):
    tag = marker()
    insert_task(
        task_title=f"MCP research {tag}", assigned_to=f"Rahul {tag}"
    )

    query = TaskQuery(
        find=TaskUpdateFind(),
        filters=TaskQueryFilters(
            title_contains=f"MCP research {tag}",
            assigned_to=f"Rahul {tag}",
        ),
    )
    monkeypatch.setattr(
        task_service, "extractor", ScriptedQueryExtractor(query)
    )

    result = task_service.process_list(
        f"show me Rahul's MCP research {tag}", employee=f"Priya {tag}"
    )

    assert result["status"] == "success"
    assert result["tasks"] == []
