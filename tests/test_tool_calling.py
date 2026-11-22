"""Tests for provider message serialization and error classification."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai.tool_calling import (
    AssistantTurn,
    CohereMessageAdapter,
    CohereToolCallingClient,
    ProviderAuthenticationError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderServerError,
    ToolCall,
    classify_provider_error,
    provider_error_message,
)


def test_assistant_turn_keeps_tool_plan_internal():
    turn = AssistantTurn(
        content="I will look that up.",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="execute_read_query",
                arguments={"sql": "SELECT id FROM tasks", "params": {}},
            )
        ],
        tool_plan="Private reasoning",
    )

    message = CohereMessageAdapter().assistant_message(turn)

    assert "tool_plan" not in message
    assert "content" not in message
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "call-1"
    assert message["tool_calls"][0]["function"]["arguments"] == (
        '{"sql": "SELECT id FROM tasks", "params": {}}'
    )


def test_text_only_assistant_turn_keeps_its_content():
    message = CohereMessageAdapter().assistant_message(
        AssistantTurn(content="Task 7 is pending.", tool_plan="internal")
    )

    assert message == {"role": "assistant", "content": "Task 7 is pending."}


def test_sanitizer_drops_internal_and_unknown_fields():
    messages = [
        {"role": "system", "content": "rules", "tool_plan": "internal"},
        {"role": "user", "content": "show my task"},
        {
            "role": "assistant",
            "content": "ignored",
            "tool_plan": "private",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect_database_schema", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"status": "ok"}'},
    ]

    sanitized = CohereMessageAdapter().sanitize_messages(messages)

    assert all("tool_plan" not in message for message in sanitized)
    assert "content" not in sanitized[2]
    assert sanitized[3]["tool_call_id"] == "call-1"


def test_tool_message_requires_a_call_id():
    adapter = CohereMessageAdapter()

    with pytest.raises(ValueError):
        adapter.tool_message(
            ToolCall(id="", name="inspect_database_schema", arguments={}),
            {"status": "success"},
        )


class _StatusError(Exception):
    def __init__(self, status_code, name=None, body=None, headers=None):
        super().__init__(name or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body or {"message": name or "provider error"}
        self.headers = headers or {}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_StatusError(400), ProviderRequestError),
        (_StatusError(422), ProviderRequestError),
        (_StatusError(401), ProviderAuthenticationError),
        (_StatusError(403), ProviderAuthenticationError),
        (_StatusError(429), ProviderRateLimitError),
        (_StatusError(500), ProviderServerError),
        (ConnectionError("connection reset"), ProviderNetworkError),
        (TimeoutError("timed out"), ProviderNetworkError),
    ],
)
def test_provider_errors_are_classified(error, expected):
    classified = classify_provider_error(error)

    assert isinstance(classified, expected)
    assert provider_error_message(classified)


def test_provider_error_retains_trace_id_without_headers():
    error = _StatusError(
        400,
        name="invalid message: tool plan cannot be used with this model",
        body={
            "message": "invalid message: tool plan cannot be used with this model",
            "trace_id": "body-trace",
        },
        headers={"x-debug-trace-id": "header-trace"},
    )

    classified = classify_provider_error(error)

    assert isinstance(classified, ProviderRequestError)
    assert classified.trace_id == "header-trace"
    assert classified.status_code == 400
    assert "tool plan" in classified.provider_detail


class _FakeCohere:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)

        if len(self.calls) == 1:
            message = SimpleNamespace(
                content=[SimpleNamespace(text="I will inspect the schema.")],
                tool_plan="Internal Cohere plan",
                tool_calls=[
                    SimpleNamespace(
                        id="call-1",
                        function=SimpleNamespace(
                            name="inspect_database_schema",
                            arguments="{}",
                        ),
                    )
                ],
            )
        else:
            message = SimpleNamespace(
                content=[SimpleNamespace(text="The schema is available.")],
                tool_plan=None,
                tool_calls=[],
            )

        return SimpleNamespace(message=message)


def test_cohere_client_does_not_send_tool_plan_on_the_second_request():
    fake = _FakeCohere()
    client = CohereToolCallingClient(client=fake)

    first = client.chat(
        messages=[{"role": "user", "content": "inspect the schema"}],
        tools=[],
    )

    assert first.tool_plan == "Internal Cohere plan"
    assert first.tool_calls[0].id == "call-1"

    messages = [
        {"role": "user", "content": "inspect the schema"},
        client.message_adapter.assistant_message(first),
        client.message_adapter.tool_message(
            first.tool_calls[0], {"status": "success"}
        ),
    ]
    second = client.chat(messages=messages, tools=[])

    assert second.content == "The schema is available."
    assert all("tool_plan" not in message for message in fake.calls[1]["messages"])


class _FlakyCohere:
    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1

        if self.calls == 1:
            raise _StatusError(500)

        return SimpleNamespace(
            message=SimpleNamespace(
                content=[SimpleNamespace(text="Recovered.")],
                tool_plan=None,
                tool_calls=[],
            )
        )


def test_transient_provider_errors_are_retried():
    fake = _FlakyCohere()
    client = CohereToolCallingClient(client=fake, retry_delays=(0,))

    turn = client.chat(messages=[{"role": "user", "content": "hello"}], tools=[])

    assert fake.calls == 2
    assert turn.content == "Recovered."


def test_invalid_requests_are_not_retried():
    fake = _FlakyCohere()

    def bad_request(**kwargs):
        fake.calls += 1
        raise _StatusError(400)

    fake.chat = bad_request
    client = CohereToolCallingClient(client=fake, retry_delays=(0,))

    with pytest.raises(ProviderRequestError):
        client.chat(messages=[{"role": "user", "content": "hello"}], tools=[])

    assert fake.calls == 1


def test_an_empty_provider_message_is_an_error():
    class _EmptyCohere:
        def chat(self, **kwargs):
            return SimpleNamespace(
                message=SimpleNamespace(
                    content=[], tool_plan=None, tool_calls=[]
                )
            )

    client = CohereToolCallingClient(client=_EmptyCohere(), retry_delays=())

    with pytest.raises(ProviderResponseError) as error:
        client.chat(messages=[{"role": "user", "content": "hello"}], tools=[])

    assert error.value.category == "invalid_response"
