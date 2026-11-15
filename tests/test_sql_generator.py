"""Tests for database/sql_generator.py: the runtime SQL generation layer.

The generator turns a validated, structured operation into a parameterised
statement, and refuses everything the schema does not allow.
"""

from __future__ import annotations

import pytest

from database.sql_generator import (
    OperationError,
    build_delete_operation,
    build_select_operation,
    build_update_operation,
    to_sql_request,
    validate_operation,
    validate_updates,
)
from database.sql_execution import validate_sql


# --- column validation ---------------------------------------------------------


def test_unknown_columns_are_rejected():
    with pytest.raises(OperationError) as error:
        validate_updates({"drop_table": "tasks"})

    assert "Unknown column" in str(error.value)


def test_an_empty_update_is_rejected():
    with pytest.raises(OperationError):
        validate_updates({})


def test_status_must_be_a_known_value():
    with pytest.raises(OperationError):
        validate_updates({"status": "DONE"})

    assert validate_updates({"status": "in_progress"})["status"] == "IN_PROGRESS"


def test_progress_must_be_a_percentage():
    with pytest.raises(OperationError):
        validate_updates({"progress": 120})

    with pytest.raises(OperationError):
        validate_updates({"progress": "many"})

    assert validate_updates({"progress": 70})["progress"] == 70


def test_dates_and_times_are_validated():
    with pytest.raises(OperationError):
        validate_updates({"deadline_date": "not a date"})

    with pytest.raises(OperationError):
        validate_updates({"assigned_time": "25:99"})

    assert str(validate_updates({"deadline_date": "2026-11-15"})["deadline_date"]) == (
        "2026-11-15"
    )


def test_the_task_title_cannot_be_empty():
    with pytest.raises(OperationError):
        validate_updates({"task_title": "   "})


# --- update operations ---------------------------------------------------------


def test_update_operation_is_parameterised():
    operation = build_update_operation(12, {"status": "IN_PROGRESS", "progress": 70})

    assert operation["intent"] == "UPDATE_TASK"
    assert operation["operation"] == "UPDATE"
    assert operation["table"] == "tasks"
    assert operation["task_id"] == 12
    assert operation["updates"] == {"status": "IN_PROGRESS", "progress": 70}
    assert ":status" in operation["sql"]
    assert ":progress" in operation["sql"]
    assert "WHERE id = :task_id" in operation["sql"]
    assert operation["params"]["task_id"] == 12
    assert "IN_PROGRESS" not in operation["sql"]


def test_update_operation_carries_the_ownership_condition():
    operation = build_update_operation(
        12, {"progress": 70}, owner="Rahul"
    )

    assert "assigned_to = :assigned_to" in operation["sql"]
    assert operation["params"]["assigned_to"] == "Rahul"


def test_update_operation_requires_a_task_id():
    with pytest.raises(OperationError):
        build_update_operation(None, {"progress": 70})

    with pytest.raises(OperationError):
        build_update_operation(0, {"progress": 70})


def test_update_operation_is_accepted_by_the_execution_layer():
    operation = build_update_operation(12, {"progress": 70})

    validate_operation(operation)

    validation = validate_sql(operation["sql"])
    assert validation.allowed
    assert validation.operation == "UPDATE"
    assert validation.requires_confirmation


# --- select / delete operations ------------------------------------------------


def test_select_operation_filters():
    operation = build_select_operation(status="pending", assigned_to="Rahul")

    assert operation["operation"] == "SELECT"
    assert operation["status"] == "PENDING"
    assert "status = :status" in operation["sql"]
    assert "assigned_to ILIKE :assigned_to" in operation["sql"]
    assert validate_sql(operation["sql"]).allowed


def test_delete_operation_is_scoped():
    operation = build_delete_operation(3, owner="Rahul")

    assert operation["operation"] == "DELETE"
    assert "WHERE id = :task_id AND assigned_to = :assigned_to" in operation["sql"]

    validation = validate_sql(operation["sql"])
    assert validation.allowed
    assert validation.requires_confirmation


def test_delete_without_a_task_id_is_rejected():
    with pytest.raises(OperationError):
        build_delete_operation(None)


# --- operation validation ------------------------------------------------------


def test_another_table_is_rejected():
    operation = build_update_operation(1, {"progress": 10})
    operation["table"] = "other"

    with pytest.raises(OperationError) as error:
        validate_operation(operation)

    assert "tasks table" in str(error.value)


def test_an_injected_statement_is_rejected():
    operation = build_update_operation(1, {"progress": 10})
    operation["sql"] = "UPDATE tasks SET progress = 10; DROP TABLE tasks"

    with pytest.raises(OperationError):
        validate_operation(operation)


def test_a_sql_statement_without_a_where_clause_is_rejected():
    operation = build_update_operation(1, {"progress": 10})
    operation["sql"] = "UPDATE tasks SET progress = 10"

    with pytest.raises(OperationError):
        validate_operation(operation)


def test_to_sql_request_matches_the_execution_contract():
    operation = build_update_operation(12, {"progress": 70})

    request = to_sql_request(operation, confirmation="CONFIRM UPDATE")

    assert request["operation"] == "UPDATE"
    assert request["params"]["task_id"] == 12
    assert request["confirmation"] == "CONFIRM UPDATE"
