"""Tests for the schema-aware read planner and the agentic read loop."""

from __future__ import annotations

import pytest

from database.read_planner import (
    OPEN_STATUSES,
    ReadFilter,
    ReadOrder,
    ReadPlan,
    ReadPlanError,
    clean_title_filter,
    compile_plan,
    plan_from_dict,
    plan_from_query,
    resolve_statuses,
    statuses_from_text,
)
from services.agentic_read import run_agentic_read


# --- status semantics ---------------------------------------------------------


def test_pending_means_open_work_in_ordinary_language():
    assert statuses_from_text("gather all pending tasks") == OPEN_STATUSES
    assert statuses_from_text("show outstanding work") == OPEN_STATUSES
    assert statuses_from_text("what is still open") == OPEN_STATUSES


def test_explicit_status_pending_stays_exact():
    assert statuses_from_text("show tasks with status pending") == ("PENDING",)
    assert statuses_from_text("only pending tasks") == OPEN_STATUSES


def test_completed_and_in_progress_statuses():
    assert statuses_from_text("list completed tasks") == ("COMPLETED",)
    assert statuses_from_text("show in progress tasks") == ("IN_PROGRESS",)


def test_negated_completion_is_open_work():
    assert statuses_from_text("show tasks that are not completed") == OPEN_STATUSES
    assert statuses_from_text("what have i not finished") == OPEN_STATUSES


def test_not_started_means_the_pending_value():
    assert statuses_from_text("show tasks that are not yet started") == ("PENDING",)
    assert statuses_from_text("tasks yet to start") == ("PENDING",)


def test_the_extracted_status_is_the_fallback():
    assert resolve_statuses("list my work", "PENDING") == ("PENDING",)
    assert resolve_statuses("gather all pending tasks", "PENDING") == OPEN_STATUSES


# --- title cleanup ------------------------------------------------------------


def test_generic_list_words_do_not_become_a_title_filter():
    assert clean_title_filter("gather all pending tasks", "gather all pending tasks") is None
    assert clean_title_filter("show me all tasks", None) is None


def test_real_title_words_are_kept():
    assert clean_title_filter("show tasks about MCP", "MCP") == "mcp"
    assert clean_title_filter("show tasks about MCP", "show tasks about MCP") == "mcp"


def test_an_assignee_is_not_mistaken_for_a_title():
    assert (
        clean_title_filter(
            "show all tasks assigned to Rahul", "Rahul", exclude=("Rahul",)
        )
        is None
    )
    assert clean_title_filter("show all tasks assigned to Rahul", None) is None


def test_a_whole_request_is_not_a_title():
    request = "gather all pending tasks for Rahul"
    assert clean_title_filter(request, request, exclude=("Rahul",)) is None


# --- plan validation and compilation ------------------------------------------


def test_unknown_columns_are_rejected():
    with pytest.raises(ReadPlanError):
        plan_from_dict({"columns": ["id"], "filters": [{"column": "secret"}]})


def test_plan_compiles_to_a_parameterized_open_status_query():
    plan = ReadPlan(
        filters=(ReadFilter("status", "in", list(OPEN_STATUSES)),),
        order_by=(ReadOrder("id", "desc"),),
        limit=25,
    )

    operation = compile_plan(plan, owner="Rahul")

    assert operation["operation"] == "SELECT"
    assert "status IN (:f0_0, :f0_1)" in operation["sql"]
    assert "assigned_to ILIKE :owner" in operation["sql"]
    assert "ORDER BY id DESC" in operation["sql"]
    assert "LIMIT 25" in operation["sql"]
    assert operation["params"]["f0_0"] == "PENDING"
    assert operation["params"]["f0_1"] == "IN_PROGRESS"
    assert operation["params"]["owner"] == "Rahul"


def test_plan_never_inlines_a_value():
    operation = plan_from_query(
        "gather all pending tasks for Rahul",
        status="PENDING",
        assigned_to="Rahul",
    )

    assert "Rahul" not in operation["sql"]
    assert "PENDING" not in operation["sql"]
    assert operation["params"]["f1"] == "%Rahul%"


# --- agentic loop -------------------------------------------------------------


class RepairingBrain:
    """Returns one invalid plan, then a valid open-task plan."""

    def __init__(self):
        self.calls = []
        self.plans = [
            ReadPlan(filters=(ReadFilter("does_not_exist", "eq", 1),)),
            ReadPlan(filters=(ReadFilter("status", "in", list(OPEN_STATUSES)),)),
        ]

    def plan(self, message, *, schema, feedback=None):
        self.calls.append(feedback)
        return self.plans.pop(0)


def test_the_agent_repairs_a_rejected_plan_and_returns_rows():
    brain = RepairingBrain()
    executed = []

    def executor(operation):
        executed.append(operation)
        return {
            "status": "success",
            "rows": [
                {"id": 7, "task_title": "Open task", "assigned_to": "Rahul",
                 "status": "IN_PROGRESS", "progress": 40}
            ],
        }

    result = run_agentic_read(
        "gather all pending tasks",
        employee="Rahul",
        brain=brain,
        executor=executor,
    )

    assert result["status"] == "success"
    assert [row["id"] for row in result["rows"]] == [7]
    assert len(executed) == 1
    assert result["attempts"][0]["error"]
    assert brain.calls[0] is None
    assert "Plan rejected" in brain.calls[1]


def test_the_agent_replans_after_an_empty_observation():
    class EmptyThenOpenBrain:
        def __init__(self):
            self.calls = []

        def plan(self, message, *, schema, feedback=None):
            self.calls.append(feedback)

            if len(self.calls) == 1:
                return ReadPlan(filters=(ReadFilter("status", "eq", "PENDING"),))

            return ReadPlan(
                filters=(ReadFilter("status", "in", list(OPEN_STATUSES)),)
            )

    brain = EmptyThenOpenBrain()

    def executor(operation):
        statuses = operation["params"].get("f0_1")

        if statuses:
            return {
                "status": "success",
                "rows": [{"id": 8, "task_title": "Open", "assigned_to": None,
                          "status": "IN_PROGRESS", "progress": 10}],
            }

        return {"status": "success", "rows": []}

    result = run_agentic_read(
        "gather all pending tasks",
        brain=brain,
        executor=executor,
        max_steps=2,
    )

    assert result["status"] == "success"
    assert result["rows"][0]["id"] == 8
    assert "returned 0 rows" in brain.calls[1]
