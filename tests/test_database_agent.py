"""Tests for the LLM-controlled database agent and its secure tools."""

from __future__ import annotations

import json

import pytest

from ai.tool_calling import AssistantTurn, ToolCall
from services import database_agent
from services.database_agent import (
    TOOL_DEFINITIONS,
    ToolContext,
    execute_tool,
    run_database_agent,
)


class ScriptedClient:
    """A deterministic tool-calling model for tests."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def chat(self, *, messages, tools):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        if not self.turns:
            return AssistantTurn(content="")
        return self.turns.pop(0)


def tool_turn(name, arguments, call_id="call-1"):
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
    )


def read_executor(rows):
    calls = []

    def _execute(sql, params, *, max_rows, timeout_ms):
        calls.append({"sql": sql, "params": params, "max_rows": max_rows})
        return {
            "status": "success",
            "operation": "SELECT",
            "rows": list(rows),
            "row_count": len(rows),
            "truncated": False,
        }

    _execute.calls = calls
    return _execute


def test_tool_definitions_are_function_schemas():
    names = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}

    assert "inspect_database_schema" in names
    assert "execute_read_query" in names
    assert "execute_write_query" in names
    assert "retrieve_conversation_context" in names

    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


def test_the_agent_reads_rows_with_a_generated_query():
    rows = [
        {
            "id": 7,
            "task_title": "Business report",
            "assigned_to": "Rahul",
            "status": "IN_PROGRESS",
            "progress": 40,
        }
    ]
    executor = read_executor(rows)
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": (
                        "SELECT id, task_title, assigned_to, status, progress "
                        "FROM tasks WHERE assigned_to ILIKE :owner"
                    ),
                    "params": {"owner": "Rahul"},
                },
            ),
            AssistantTurn(content="The task title is Business report."),
        ]
    )

    result = run_database_agent(
        "what is the task title?",
        employee="Rahul",
        client=client,
        read_executor=executor,
    )

    assert result["status"] == "success"
    assert result["intent"] == "GET_TASK"
    assert result["task"]["id"] == 7
    assert result["requested_field"] == "task_title"
    assert result["reply"] == "Business report"
    assert result["planner"] == "llm_agent"
    assert result["agent"]["tools"][0]["tool"] == "execute_read_query"
    assert executor.calls[0]["params"] == {"owner": "Rahul"}


def test_a_single_requested_field_drops_model_narration_and_control_tokens():
    rows = [
        {
            "id": 11,
            "task_title": "Release report",
            "task_description": "Confirmed deployment summary",
            "assigned_to": "Rahul",
        }
    ]
    executor = read_executor(rows)
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": (
                        "SELECT id, task_title, task_description, assigned_to "
                        "FROM tasks WHERE assigned_to ILIKE :owner"
                    ),
                    "params": {"owner": "Rahul"},
                },
            ),
            AssistantTurn(
                content=(
                    '<EOS_TOKEN>The task description is "Confirmed deployment '
                    'summary". I should answer with just the description as '
                    "per the rules.Confirmed deployment summary"
                )
            ),
        ]
    )

    result = run_database_agent(
        "What is its description?",
        employee="Rahul",
        client=client,
        read_executor=executor,
    )

    assert result["status"] == "success"
    assert result["requested_field"] == "task_description"
    assert result["reply"] == "Confirmed deployment summary"
    assert "<EOS_TOKEN>" not in result["reply"]


def test_a_field_answered_from_memory_is_re_read_and_formatted(monkeypatch):
    memory = {
        "active_task_id": 13,
        "context": {"active_task": {"id": 13, "task_title": "Release report"}},
    }
    monkeypatch.setattr(
        database_agent.task_repository,
        "get_task_for_owner",
        lambda task_id, owner: {
            "id": task_id,
            "task_title": "Release report",
            "task_description": "Fresh description from PostgreSQL",
            "assigned_to": owner,
        },
    )
    client = ScriptedClient(
        [
            AssistantTurn(
                content=(
                    "The user wants the description. "
                    "The description is Fresh description from PostgreSQL."
                )
            )
        ]
    )

    result = run_database_agent(
        "What is its description?",
        employee="Rahul",
        memory=memory,
        client=client,
    )

    assert result["status"] == "success"
    assert result["requested_field"] == "task_description"
    assert result["reply"] == "Fresh description from PostgreSQL"
    assert result["agent"]["iterations"] == 1


def test_a_multi_field_question_keeps_the_model_answer():
    rows = [
        {
            "id": 12,
            "task_title": "Release report",
            "assigned_to": "Rahul",
            "status": "IN_PROGRESS",
            "progress": 40,
        }
    ]
    executor = read_executor(rows)
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": (
                        "SELECT id, task_title, assigned_to, status, progress "
                        "FROM tasks WHERE assigned_to ILIKE :owner"
                    ),
                    "params": {"owner": "Rahul"},
                },
            ),
            AssistantTurn(content="IN_PROGRESS at 40%."),
        ]
    )

    result = run_database_agent(
        "What are its status and progress?",
        employee="Rahul",
        client=client,
        read_executor=executor,
    )

    assert result["reply"] == "IN_PROGRESS at 40%."
    assert "requested_field" not in result


def test_context_is_available_to_the_model_and_a_context_tool(monkeypatch):
    memory = {
        "active_task_id": 9,
        "last_action": "GET_TASK",
        "context": {
            "active_task": {"id": 9, "task_title": "Earlier task"},
            "candidates": [{"id": 9, "task_title": "Earlier task"}],
            "history": [{"role": "user", "text": "get task 9"}],
            "agent_trace": [{"tool": "execute_read_query", "status": "success"}],
        },
    }

    monkeypatch.setattr(
        database_agent.task_repository,
        "get_task_for_owner",
        lambda task_id, owner: None,
    )

    client = ScriptedClient(
        [
            tool_turn("retrieve_conversation_context", {}),
            AssistantTurn(content="The active task is 9."),
        ]
    )

    result = run_database_agent(
        "what about its status?",
        employee="Rahul",
        memory=memory,
        client=client,
    )

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert "Earlier task" in system_prompt
    assert "execute_read_query" in system_prompt

    tool_result = json.loads(client.calls[1]["messages"][-1]["content"])
    assert tool_result["active_task_id"] == 9
    assert tool_result["candidates"][0]["id"] == 9
    assert result["reply"] == "The active task is 9."


def test_a_rejected_query_is_repaired_after_the_observation():
    rows = [{"id": 3, "task_title": "Fixed", "assigned_to": "Rahul"}]
    executor = read_executor(rows)
    client = ScriptedClient(
        [
            tool_turn("execute_read_query", {"sql": "TRUNCATE TABLE tasks"}),
            tool_turn(
                "execute_read_query",
                {
                    "sql": (
                        "SELECT id, task_title, assigned_to FROM tasks "
                        "WHERE assigned_to ILIKE :owner"
                    ),
                    "params": {"owner": "Rahul"},
                },
                call_id="call-2",
            ),
            AssistantTurn(content="I found the fixed task."),
        ]
    )

    result = run_database_agent(
        "show my tasks", employee="Rahul", client=client, read_executor=executor
    )

    assert result["status"] == "success"
    assert result["task"]["id"] == 3
    assert result["agent"]["tools"][0]["status"] == "error"
    assert result["agent"]["tools"][1]["status"] == "success"
    assert len(executor.calls) == 1


def test_a_database_error_is_observed_and_retried():
    calls = []

    def executor(sql, params, *, max_rows, timeout_ms):
        calls.append(sql)
        if "missing_column" in sql:
            raise ValueError("column missing_column does not exist")
        return {
            "status": "success",
            "operation": "SELECT",
            "rows": [{"id": 4, "assigned_to": "Rahul"}],
            "row_count": 1,
            "truncated": False,
        }

    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": "SELECT missing_column, assigned_to FROM tasks",
                    "params": {},
                },
            ),
            tool_turn(
                "execute_read_query",
                {
                    "sql": "SELECT id, assigned_to FROM tasks",
                    "params": {},
                },
                call_id="call-2",
            ),
            AssistantTurn(content="Task 4 is the one."),
        ]
    )

    result = run_database_agent(
        "show the task", employee="Rahul", client=client, read_executor=executor
    )

    assert result["task"]["id"] == 4
    assert "does not exist" in result["agent"]["tools"][0]["message"]
    assert len(calls) == 2


def test_rows_are_filtered_to_the_employee_scope():
    executor = read_executor(
        [
            {"id": 1, "assigned_to": "Rahul", "task_title": "Mine"},
            {"id": 2, "assigned_to": "Priya", "task_title": "Not mine"},
        ]
    )
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": "SELECT id, task_title, assigned_to FROM tasks",
                    "params": {},
                },
            ),
            AssistantTurn(content="I found your task."),
        ]
    )

    result = run_database_agent(
        "show tasks", employee="Rahul", client=client, read_executor=executor
    )

    assert [task["id"] for task in result["tasks"]] == [1]


def test_a_read_without_an_ownership_column_is_rejected():
    executor = read_executor([{"id": 1, "task_title": "Mine"}])
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {"sql": "SELECT id, task_title FROM tasks", "params": {}},
            ),
            AssistantTurn(content="I could not scope that safely."),
        ]
    )

    result = run_database_agent(
        "show tasks", employee="Rahul", client=client, read_executor=executor
    )

    assert result["agent"]["tools"][0]["status"] == "error"
    assert "assigned_to" in result["agent"]["tools"][0]["message"]


def test_a_write_requires_the_exact_confirmation_and_does_not_execute_early():
    calls = []

    def write_executor(sql, params, *, confirmation, operation):
        calls.append(
            {
                "sql": sql,
                "params": params,
                "confirmation": confirmation,
                "operation": operation,
            }
        )
        return {
            "status": "success",
            "operation": "DELETE",
            "rows": [{"id": 7, "task_title": "Gone", "assigned_to": "Rahul"}],
        }

    delete_call = {
        "sql": (
            "DELETE FROM tasks WHERE id = :id "
            "AND assigned_to ILIKE :employee RETURNING id, task_title, assigned_to"
        ),
        "params": {"id": 7, "employee": "Rahul"},
    }

    client = ScriptedClient(
        [
            tool_turn("execute_write_query", delete_call),
            AssistantTurn(content="Reply CONFIRM DELETE to remove it."),
        ]
    )

    result = run_database_agent(
        "delete task 7",
        employee="Rahul",
        client=client,
        write_executor=write_executor,
        confirmation="delete task 7",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_phrase"] == "CONFIRM DELETE"
    assert calls == []

    client = ScriptedClient(
        [
            tool_turn("execute_write_query", delete_call),
            AssistantTurn(content="Task 7 was deleted."),
        ]
    )

    result = run_database_agent(
        "CONFIRM DELETE",
        employee="Rahul",
        client=client,
        write_executor=write_executor,
        confirmation="CONFIRM DELETE",
    )

    assert result["status"] == "success"
    assert result["intent"] == "DELETE_TASK"
    assert len(calls) == 1
    assert calls[0]["confirmation"] == "CONFIRM DELETE"


def test_an_employee_scoped_write_needs_an_ownership_predicate():
    writes = []

    def write_executor(sql, params, *, confirmation, operation):
        writes.append(sql)
        return {"status": "success", "operation": operation, "rows": []}

    client = ScriptedClient(
        [
            tool_turn(
                "execute_write_query",
                {
                    "sql": "DELETE FROM tasks WHERE id = :id RETURNING id",
                    "params": {"id": 7},
                },
            ),
            AssistantTurn(content="I need to restrict that write first."),
        ]
    )

    result = run_database_agent(
        "delete task 7",
        employee="Rahul",
        client=client,
        write_executor=write_executor,
        confirmation="CONFIRM DELETE",
    )

    assert result["agent"]["tools"][0]["status"] == "error"
    assert writes == []


def test_duplicate_tool_calls_are_not_repeated():
    executor = read_executor([{"id": 1, "assigned_to": "Rahul"}])
    call = {
        "sql": "SELECT id, assigned_to FROM tasks",
        "params": {},
    }
    client = ScriptedClient(
        [
            tool_turn("execute_read_query", call),
            tool_turn("execute_read_query", call, call_id="call-2"),
            AssistantTurn(content="Task 1 is yours."),
        ]
    )

    result = run_database_agent(
        "show tasks", employee="Rahul", client=client, read_executor=executor
    )

    assert len(executor.calls) == 1
    assert result["agent"]["tools"][1]["status"] == "error"
    assert "already made" in result["agent"]["tools"][1]["message"]


def test_the_loop_is_bounded(monkeypatch):
    executor = read_executor([{"id": 1, "assigned_to": "Rahul"}])
    turns = [
        tool_turn(
            "execute_read_query",
            {"sql": "SELECT id, assigned_to FROM tasks", "params": {}},
            call_id=f"call-{index}",
        )
        for index in range(5)
    ]
    client = ScriptedClient(turns)

    result = run_database_agent(
        "show tasks",
        employee="Rahul",
        client=client,
        read_executor=executor,
        max_iterations=2,
    )

    assert result["status"] == "success"
    assert result["agent"]["iterations"] == 2
    assert "within the allowed number of steps" in result["reply"]


def test_schema_and_table_tools_use_the_live_model():
    schema = execute_tool("inspect_database_schema", {}, ToolContext("x"))
    table = execute_tool(
        "inspect_table_definition", {"table": "tasks"}, ToolContext("x")
    )
    other = execute_tool(
        "inspect_table_definition", {"table": "secrets"}, ToolContext("x")
    )

    assert "task_title" in schema["schema"]
    assert {column["name"] for column in table["columns"]} >= {
        "id",
        "task_title",
        "assigned_to",
        "status",
        "progress",
    }
    assert other["status"] == "error"


def test_the_chat_turn_uses_the_agent(monkeypatch):
    from services import task_service

    rows = [{"id": 2, "task_title": "LangChain", "assigned_to": "Rahul"}]
    executor = read_executor(rows)
    client = ScriptedClient(
        [
            tool_turn(
                "execute_read_query",
                {
                    "sql": (
                        "SELECT id, task_title, assigned_to FROM tasks "
                        "WHERE assigned_to ILIKE :owner"
                    ),
                    "params": {"owner": "Rahul"},
                },
            ),
            AssistantTurn(content="The task is LangChain."),
        ]
    )

    monkeypatch.setenv("DATABASE_AGENT", "1")
    monkeypatch.setattr(database_agent, "default_client", lambda: client)
    monkeypatch.setattr(
        database_agent.task_repository, "execute_read_sql", executor
    )

    result = task_service.process_chat_turn(
        "show my tasks", employee="Rahul"
    )

    assert result["planner"] == "llm_agent"
    assert result["reply"] == "The task is LangChain."
    assert result["task"]["id"] == 2


def test_the_agent_is_disabled_without_the_flag(monkeypatch):
    monkeypatch.delenv("DATABASE_AGENT", raising=False)

    assert run_database_agent("show tasks") is None


def test_tool_plan_is_internal_and_never_sent_back_to_the_provider():
    executor = read_executor([{"id": 2, "assigned_to": "Rahul"}])
    plan_turn = tool_turn(
        "execute_read_query",
        {"sql": "SELECT id, assigned_to FROM tasks", "params": {}},
    )
    plan_turn.tool_plan = "Private chain of thought"
    client = ScriptedClient(
        [plan_turn, AssistantTurn(content="Task 2 is yours.")]
    )

    result = run_database_agent(
        "show tasks", employee="Rahul", client=client, read_executor=executor
    )

    assert result["status"] == "success"
    second_request = client.calls[1]["messages"]

    assert all("tool_plan" not in message for message in second_request)

    assistant = next(
        message for message in second_request if message["role"] == "assistant"
    )
    assert "content" not in assistant
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["tool_calls"][0]["function"]["name"] == "execute_read_query"


def test_multiple_consecutive_tool_calls_keep_their_call_ids():
    client = ScriptedClient(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call-a",
                        name="inspect_database_schema",
                        arguments={},
                    ),
                    ToolCall(
                        id="",
                        name="retrieve_conversation_context",
                        arguments={},
                    ),
                ]
            ),
            AssistantTurn(content="I inspected the schema and context."),
        ]
    )

    result = run_database_agent(
        "what can you see?",
        employee="Rahul",
        conversation_id="conv-1",
        client=client,
    )

    assert result["status"] == "success"
    messages = client.calls[1]["messages"]
    roles = [message["role"] for message in messages]
    assistant_index = roles.index("assistant")
    assistant = messages[assistant_index]
    tool_messages = messages[assistant_index + 1 :]

    assert len(assistant["tool_calls"]) == 2
    assert [message["role"] for message in tool_messages] == ["tool", "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call-a",
        assistant["tool_calls"][1]["id"],
    ]
    assert assistant["tool_calls"][1]["id"]
    assert len(result["agent"]["tools"]) == 2


def test_invalid_tool_arguments_are_rejected_before_execution():
    client = ScriptedClient(
        [
            tool_turn(
                "inspect_table_definition",
                {"table": "tasks", "unexpected": "value"},
            ),
            AssistantTurn(content="I need to correct the tool call."),
        ]
    )

    result = run_database_agent("inspect tasks", client=client)

    assert result["status"] == "success"
    assert result["agent"]["tools"][0]["status"] == "error"
    assert "Unknown tool argument" in result["agent"]["tools"][0]["message"]


class _ProviderBadRequest(Exception):
    status_code = 400
    headers = {"x-debug-trace-id": "trace-123"}
    body = {"message": "tool plan cannot be used with this model"}


class _FailingProviderClient:
    def __init__(self):
        self.calls = 0

    def chat(self, *, messages, tools):
        self.calls += 1
        raise _ProviderBadRequest("invalid request")


def test_provider_failure_is_not_reported_as_success_and_is_not_retried():
    client = _FailingProviderClient()

    result = run_database_agent(
        "show tasks",
        employee="Rahul",
        conversation_id="conv-123",
        client=client,
    )

    assert client.calls == 1
    assert result["status"] == "error"
    assert result["error_category"] == "invalid_request"
    assert result["agent"]["error_category"] == "invalid_request"
    assert result["agent"]["provider_trace_id"] == "trace-123"
    assert "rejected" in result["reply"]
