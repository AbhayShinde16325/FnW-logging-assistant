"""Tests for database/sql_execution.py: validation and confirmation gating."""

from __future__ import annotations

import pytest

from database.sql_execution import (
    confirmation_phrase,
    execute_sql,
    plan_sql,
    run_sql_request,
    validate_confirmation,
    validate_sql,
)


class FakeConnection:
    """Record every execute() call so tests can assert nothing ran."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        return "ok"


# --- Operation policy ----------------------------------------------------------

def test_select_is_allowed_without_confirmation():
    validation = validate_sql("SELECT id, task_title FROM tasks WHERE id = :id")

    assert validation.allowed
    assert validation.operation == "SELECT"
    assert validation.table == "tasks"
    assert not validation.requires_confirmation


def test_insert_is_allowed_without_confirmation():
    validation = validate_sql(
        "INSERT INTO tasks (task_title) VALUES (:task_title) RETURNING id"
    )

    assert validation.allowed
    assert validation.operation == "INSERT"
    assert validation.table == "tasks"
    assert not validation.requires_confirmation


def test_update_requires_confirmation():
    validation = validate_sql(
        "UPDATE tasks SET progress = :progress WHERE id = :id"
    )

    assert validation.allowed
    assert validation.operation == "UPDATE"
    assert validation.requires_confirmation


def test_delete_requires_confirmation():
    validation = validate_sql("DELETE FROM tasks WHERE id = :id")

    assert validation.allowed
    assert validation.operation == "DELETE"
    assert validation.requires_confirmation


def test_drop_table_requires_confirmation():
    validation = validate_sql("DROP TABLE tasks")

    assert validation.allowed
    assert validation.operation == "DROP TABLE"
    assert validation.table == "tasks"
    assert validation.requires_confirmation


def test_bare_select_is_allowed():
    validation = validate_sql("SELECT 1")

    assert validation.allowed
    assert validation.operation == "SELECT"


def test_canonical_query_templates_validate():
    templates = [
        "SELECT id, task_title, assigned_to, status, progress FROM tasks ORDER BY id",
        "SELECT id, task_title, status FROM tasks WHERE assigned_to ILIKE :assignee ORDER BY id",
        "INSERT INTO tasks (task_title, assigned_to, status, progress) "
        "VALUES (:task_title, :assigned_to, :status, :progress) RETURNING id",
        "UPDATE tasks SET progress = :progress, status = :status WHERE id = :id "
        "RETURNING id, task_title, status, progress",
        "DELETE FROM tasks WHERE id = :id RETURNING id, task_title, status, progress",
        "DROP TABLE tasks",
    ]

    for sql in templates:
        validation = validate_sql(sql)
        assert validation.allowed, sql
        assert validation.table == "tasks"


# --- Validation failures -------------------------------------------------------

def test_update_without_where_is_rejected():
    validation = validate_sql("UPDATE tasks SET progress = :progress")

    assert not validation.allowed
    assert "WHERE" in validation.reason


def test_delete_without_where_is_rejected():
    validation = validate_sql("DELETE FROM tasks")

    assert not validation.allowed
    assert "WHERE" in validation.reason


def test_tautological_where_is_rejected():
    validation = validate_sql("DELETE FROM tasks WHERE 1 = 1")

    assert not validation.allowed
    assert "specific WHERE" in validation.reason


def test_select_into_is_rejected():
    validation = validate_sql("SELECT id INTO other FROM tasks")

    assert not validation.allowed


def test_select_for_update_is_rejected():
    validation = validate_sql("SELECT id FROM tasks WHERE id = :id FOR UPDATE")

    assert not validation.allowed


def test_join_is_rejected():
    validation = validate_sql(
        "SELECT t.id FROM tasks t JOIN other o ON o.id = t.id"
    )

    assert not validation.allowed


def test_other_table_is_rejected():
    validation = validate_sql("SELECT id FROM other")

    assert not validation.allowed
    assert "tasks" in validation.reason


def test_truncate_is_prohibited():
    validation = validate_sql("TRUNCATE TABLE tasks")

    assert not validation.allowed
    assert validation.operation is None


def test_alter_is_prohibited():
    validation = validate_sql("ALTER TABLE tasks ADD COLUMN note varchar")

    assert not validation.allowed
    assert validation.operation is None


def test_create_database_is_prohibited():
    validation = validate_sql("CREATE DATABASE test")

    assert not validation.allowed
    assert validation.operation is None


def test_drop_table_cascade_is_prohibited():
    validation = validate_sql("DROP TABLE tasks CASCADE")

    assert not validation.allowed


def test_drop_other_table_is_rejected():
    validation = validate_sql("DROP TABLE other")

    assert not validation.allowed


def test_multiple_statements_are_rejected():
    validation = validate_sql("SELECT 1; DROP TABLE tasks;")

    assert not validation.allowed
    assert "one SQL statement" in validation.reason


def test_operation_mismatch_is_rejected():
    connection = FakeConnection()

    result = execute_sql(
        "DELETE FROM tasks WHERE id = :id",
        {"id": 7},
        operation="UPDATE",
        confirmation="CONFIRM UPDATE",
        connection=connection,
    )

    assert result["status"] == "error"
    assert connection.calls == []


def test_empty_sql_is_rejected():
    validation = validate_sql("   ")

    assert not validation.allowed


# --- Confirmation rules --------------------------------------------------------

def test_confirmation_phrases_are_exact():
    assert confirmation_phrase("SELECT") is None
    assert confirmation_phrase("INSERT") is None
    assert confirmation_phrase("UPDATE") == "CONFIRM UPDATE"
    assert confirmation_phrase("DELETE") == "CONFIRM DELETE"
    assert confirmation_phrase("DROP TABLE") == "CONFIRM DROP TABLE tasks"


def test_exact_confirmation_is_accepted_case_insensitively():
    assert validate_confirmation("DELETE", "confirm delete")
    assert validate_confirmation("DROP TABLE", "confirm drop table tasks")


def test_vague_confirmation_is_rejected():
    assert not validate_confirmation("DELETE", "yes")
    assert not validate_confirmation("DELETE", "ok")
    assert not validate_confirmation("DELETE", "sure")
    assert not validate_confirmation("DELETE", "go ahead")
    assert not validate_confirmation("DELETE", None)


def test_wrong_confirmation_phrase_is_rejected():
    assert not validate_confirmation("DELETE", "CONFIRM UPDATE")
    assert not validate_confirmation("UPDATE", "CONFIRM DELETE")
    assert not validate_confirmation("DROP TABLE", "CONFIRM DELETE")


# --- Execution safeguards ------------------------------------------------------

def test_execute_select_passes_parameters_unchanged():
    connection = FakeConnection()

    result = execute_sql(
        "SELECT id, task_title FROM tasks WHERE id = :id",
        {"id": 7},
        connection=connection,
    )

    assert result["status"] == "success"
    assert connection.calls == [
        ("SELECT id, task_title FROM tasks WHERE id = :id", {"id": 7})
    ]


def test_execute_delete_without_confirmation_does_not_touch_connection():
    connection = FakeConnection()

    result = execute_sql(
        "DELETE FROM tasks WHERE id = :id",
        {"id": 7},
        connection=connection,
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_phrase"] == "CONFIRM DELETE"
    assert connection.calls == []


def test_execute_delete_with_confirmation_executes_once():
    connection = FakeConnection()

    result = execute_sql(
        "DELETE FROM tasks WHERE id = :id",
        {"id": 7},
        confirmation="CONFIRM DELETE",
        connection=connection,
    )

    assert result["status"] == "success"
    assert len(connection.calls) == 1


def test_execute_drop_table_with_wrong_confirmation_does_not_execute():
    connection = FakeConnection()

    result = execute_sql(
        "DROP TABLE tasks",
        confirmation="CONFIRM DELETE",
        connection=connection,
    )

    assert result["status"] == "confirmation_required"
    assert connection.calls == []


def test_execute_drop_table_with_exact_confirmation_executes():
    connection = FakeConnection()

    result = execute_sql(
        "DROP TABLE tasks",
        confirmation="CONFIRM DROP TABLE tasks",
        connection=connection,
    )

    assert result["status"] == "success"
    assert len(connection.calls) == 1


def test_execute_prohibited_operation_returns_error_without_connection():
    result = execute_sql("TRUNCATE TABLE tasks")

    assert result["status"] == "error"
    assert result["operation"] is None


def test_execute_requires_connection_when_confirmation_is_valid():
    with pytest.raises(ValueError):
        execute_sql(
            "DELETE FROM tasks WHERE id = :id",
            {"id": 7},
            confirmation="CONFIRM DELETE",
        )


# --- Plan and structured request ------------------------------------------------

def test_plan_sql_reports_confirmation_phrase():
    plan = plan_sql("DELETE FROM tasks WHERE id = :id", {"id": 7})

    assert plan["status"] == "plan"
    assert plan["operation"] == "DELETE"
    assert plan["requires_confirmation"] is True
    assert plan["confirmation_phrase"] == "CONFIRM DELETE"
    assert plan["target_table"] == "tasks"


def test_plan_sql_for_select_has_no_confirmation_phrase():
    plan = plan_sql("SELECT id FROM tasks WHERE id = :id", {"id": 7})

    assert plan["requires_confirmation"] is False
    assert plan["confirmation_phrase"] is None


def test_plan_sql_for_prohibited_operation_is_error():
    plan = plan_sql("TRUNCATE TABLE tasks")

    assert plan["status"] == "error"


def test_run_sql_request_requires_sql():
    result = run_sql_request({})

    assert result["status"] == "error"


def test_run_sql_request_returns_confirmation_required_shape():
    request = {
        "operation": "DELETE",
        "sql": "DELETE FROM tasks WHERE id = :id",
        "params": {"id": 7},
        "confirmation": None,
    }

    result = run_sql_request(request)

    assert result["status"] == "confirmation_required"
    assert result["confirmation_phrase"] == "CONFIRM DELETE"
    assert result["target_table"] == "tasks"
    assert result["impact"]


def test_run_sql_request_executes_after_exact_confirmation():
    connection = FakeConnection()
    request = {
        "operation": "DELETE",
        "sql": "DELETE FROM tasks WHERE id = :id",
        "params": {"id": 7},
        "confirmation": "CONFIRM DELETE",
    }

    result = run_sql_request(request, connection=connection)

    assert result["status"] == "success"
    assert len(connection.calls) == 1


def test_run_sql_request_rejects_mismatched_operation():
    request = {
        "operation": "UPDATE",
        "sql": "DELETE FROM tasks WHERE id = :id",
        "params": {"id": 7},
    }

    result = run_sql_request(request)

    assert result["status"] == "error"


def test_run_sql_request_rejects_prohibited_operation():
    request = {
        "operation": "SELECT",
        "sql": "TRUNCATE TABLE tasks",
    }

    result = run_sql_request(request)

    assert result["status"] == "error"
