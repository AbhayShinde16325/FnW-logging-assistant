"""Native tool-calling client for the database agent.

The agent loop is provider-independent: it only needs an object with a
``chat(messages, tools)`` method that returns an :class:`AssistantTurn`. The
production implementation uses the existing Cohere client and its native
function/tool calling. Tests inject a small scripted client instead.

Internal turn metadata (for example Cohere's ``tool_plan``) is kept on the
:class:`AssistantTurn` for tracing, but it is never serialized into a provider
message. Provider messages are built by an explicit adapter so a response
field can never leak back into a request by accident.

No database access happens here. The tool dispatcher in
``services.database_agent`` executes the validated tools.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MODEL_NAME = "command-a-plus-05-2026"


# --- provider errors --------------------------------------------------------


class ProviderError(Exception):
    """A safe, classified failure of the model provider.

    ``category`` is suitable for logs and for choosing a user-facing message.
    ``provider_detail`` is the provider's message without headers or
    credentials; it is only written to the server log.
    """

    category = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        trace_id: str | None = None,
        provider_detail: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.trace_id = trace_id
        self.provider_detail = provider_detail or message


class ProviderRequestError(ProviderError):
    """The provider rejected the request. Retrying it unchanged is useless."""

    category = "invalid_request"


class ProviderAuthenticationError(ProviderError):
    category = "authentication"


class ProviderRateLimitError(ProviderError):
    category = "rate_limit"
    retryable = True


class ProviderServerError(ProviderError):
    category = "server_error"
    retryable = True


class ProviderNetworkError(ProviderError):
    category = "network"
    retryable = True


class ProviderResponseError(ProviderError):
    category = "invalid_response"


PROVIDER_ERROR_MESSAGES = {
    "invalid_request": (
        "The assistant request was rejected by the AI provider. "
        "The request was not retried."
    ),
    "authentication": (
        "The assistant cannot authenticate with the AI provider. "
        "Please check the provider configuration."
    ),
    "rate_limit": (
        "The assistant is temporarily rate-limited by the AI provider. "
        "Please try again shortly."
    ),
    "server_error": (
        "The AI provider is temporarily unavailable. Please try again shortly."
    ),
    "network": (
        "The assistant could not reach the AI provider. Please try again."
    ),
    "invalid_response": (
        "The AI provider returned an unreadable response. "
        "The request was not retried."
    ),
    "provider_error": (
        "The assistant could not complete the model request. Please try again."
    ),
}


def provider_error_message(error: ProviderError) -> str:
    return PROVIDER_ERROR_MESSAGES.get(
        error.category, PROVIDER_ERROR_MESSAGES["provider_error"]
    )


def _trace_id(headers, body) -> str | None:
    values = headers if isinstance(headers, dict) else {}

    for key in ("x-debug-trace-id", "x-request-id", "traceparent"):
        value = values.get(key)

        if value:
            return str(value).strip()[:200]

    if isinstance(body, dict):
        for key in ("trace_id", "request_id", "id"):
            value = body.get(key)

            if value:
                return str(value).strip()[:200]

    return None


def _provider_detail(body) -> str:
    """The provider's message, without headers or credentials."""

    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            value = body.get(key)

            if value:
                return str(value).strip()[:500]

        return ""

    if isinstance(body, str):
        return body.strip()[:500]

    return ""


def classify_provider_error(error: Exception) -> ProviderError:
    """Turn a provider/client exception into a stable error category."""

    if isinstance(error, ProviderError):
        return error

    status = getattr(error, "status_code", None)

    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    name = type(error).__name__.lower()
    headers = getattr(error, "headers", None)
    body = getattr(error, "body", None)
    detail = _provider_detail(body) or str(error).strip()[:500]
    trace = _trace_id(headers, body)

    if status in (400, 404, 409, 422) or "badrequest" in name:
        error_class = ProviderRequestError
    elif status in (401, 403) or any(
        token in name
        for token in ("unauthorized", "forbidden", "invalidtoken")
    ):
        error_class = ProviderAuthenticationError
    elif status == 429 or "toomanyrequests" in name:
        error_class = ProviderRateLimitError
    elif (status is not None and 500 <= status < 600) or any(
        token in name
        for token in ("internalserver", "serviceunavailable", "gatewaytimeout")
    ):
        error_class = ProviderServerError
    elif isinstance(error, (TimeoutError, ConnectionError, OSError)) or any(
        token in name for token in ("connect", "timeout", "network", "transport")
    ):
        error_class = ProviderNetworkError
    else:
        error_class = ProviderError

    return error_class(
        detail or name,
        status_code=status,
        trace_id=trace,
        provider_detail=detail,
    )


# --- internal turn and provider messages ------------------------------------


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Internal metadata. It is deliberately never put into a provider message.
    tool_plan: str | None = None

    def as_message(self) -> dict:
        """A safe, provider-neutral assistant message.

        ``tool_plan`` stays internal. Text is not combined with tool calls,
        because Cohere rejects that shape.
        """

        return CohereMessageAdapter().assistant_message(self)


def _serialize_tool_call(call: ToolCall | dict) -> dict:
    if isinstance(call, ToolCall):
        call_id = call.id
        name = call.name
        arguments = call.arguments
    else:
        call = call or {}
        call_id = call.get("id")
        function = call.get("function") or {}
        name = function.get("name")
        arguments = function.get("arguments")

    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, default=str)
    elif not isinstance(arguments, str):
        arguments = "{}"

    return {
        "id": str(call_id or ""),
        "type": "function",
        "function": {
            "name": str(name or ""),
            "arguments": arguments,
        },
    }


class CohereMessageAdapter:
    """Serialize internal turns into Cohere-compatible chat messages.

    Only the fields defined by Cohere's Chat v2 message schema are emitted.
    Internal fields such as ``tool_plan`` are intentionally dropped.
    """

    def assistant_message(self, turn: AssistantTurn) -> dict:
        message: dict[str, Any] = {"role": "assistant"}

        if turn.tool_calls:
            message["tool_calls"] = [
                _serialize_tool_call(call) for call in turn.tool_calls
            ]
        else:
            message["content"] = turn.content or ""

        return message

    def tool_message(self, call: ToolCall, result: Any) -> dict:
        if not str(call.id or "").strip():
            raise ValueError("A tool call id is required for a tool result.")

        content = (
            result
            if isinstance(result, str)
            else json.dumps(result, default=str, ensure_ascii=False)
        )

        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": content,
        }

    def sanitize_messages(self, messages: list[dict]) -> list[dict]:
        """Drop internal/unknown fields before a provider request."""

        sanitized = []

        for message in messages or []:
            if not isinstance(message, dict):
                continue

            role = str(message.get("role") or "").strip().lower()

            if role in ("system", "user"):
                sanitized.append(
                    {
                        "role": role,
                        "content": message.get("content") or "",
                    }
                )
                continue

            if role == "assistant":
                calls = message.get("tool_calls")

                if isinstance(calls, list) and calls:
                    sanitized.append(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                _serialize_tool_call(call)
                                for call in calls
                                if isinstance(call, (ToolCall, dict))
                            ],
                        }
                    )
                else:
                    sanitized.append(
                        {
                            "role": "assistant",
                            "content": message.get("content") or "",
                        }
                    )
                continue

            if role == "tool":
                content = message.get("content")
                sanitized.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(
                            message.get("tool_call_id") or ""
                        ),
                        "content": (
                            content
                            if isinstance(content, (str, list))
                            else json.dumps(
                                content, default=str, ensure_ascii=False
                            )
                        ),
                    }
                )

        return sanitized


def default_message_adapter(client=None) -> CohereMessageAdapter:
    adapter = getattr(client, "message_adapter", None)

    if isinstance(adapter, CohereMessageAdapter):
        return adapter

    return CohereMessageAdapter()


class ToolCallingClient(Protocol):
    def chat(self, *, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        ...


def _content_text(message) -> str:
    """Join the text segments of a Cohere assistant message."""

    content = getattr(message, "content", None)

    if isinstance(content, str):
        return content.strip()

    parts = []

    for segment in content or []:
        text = getattr(segment, "text", None)
        if text:
            parts.append(str(text))

    return "\n".join(parts).strip()


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"_invalid_json": raw}

        return value if isinstance(value, dict) else {"_invalid_arguments": value}

    return {}


class CohereToolCallingClient:
    """Native Cohere tool calling using the project model."""

    def __init__(
        self,
        client=None,
        model: str = MODEL_NAME,
        retry_delays: tuple[float, ...] = (0.25, 0.5),
    ):
        self._client = client
        self._model = model
        self._retry_delays = tuple(retry_delays)
        self.message_adapter = CohereMessageAdapter()

    def _get_client(self):
        if self._client is not None:
            return self._client

        import cohere

        api_key = os.getenv("COHERE_API_KEY")

        if not api_key:
            raise RuntimeError("COHERE_API_KEY is not configured.")

        self._client = cohere.ClientV2(api_key=api_key)
        return self._client

    def chat(self, *, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        request_messages = self.message_adapter.sanitize_messages(messages)
        started = monotonic()
        attempts = len(self._retry_delays) + 1

        for attempt in range(1, attempts + 1):
            attempt_started = monotonic()

            try:
                response = self._get_client().chat(
                    model=self._model,
                    messages=request_messages,
                    tools=tools,
                    temperature=0,
                    thinking={"type": "disabled"},
                )
            except Exception as error:
                classified = classify_provider_error(error)
                duration_ms = round((monotonic() - attempt_started) * 1000)
                can_retry = (
                    classified.retryable and attempt < attempts
                )

                logger.warning(
                    "Cohere request failed: model=%s category=%s status=%s "
                    "trace_id=%s attempt=%s retrying=%s detail=%s",
                    self._model,
                    classified.category,
                    classified.status_code,
                    classified.trace_id,
                    attempt,
                    can_retry,
                    classified.provider_detail,
                    extra={
                        "event": "cohere_model_request",
                        "model_status": "error",
                        "error_category": classified.category,
                        "provider_status": classified.status_code,
                        "provider_trace_id": classified.trace_id,
                        "attempt": attempt,
                        "retrying": can_retry,
                        "duration_ms": duration_ms,
                    },
                )

                if can_retry:
                    sleep(self._retry_delays[attempt - 1])
                    continue

                raise classified from error

            logger.info(
                "Cohere request completed: model=%s attempt=%s duration_ms=%s",
                self._model,
                attempt,
                round((monotonic() - started) * 1000),
                extra={
                    "event": "cohere_model_request",
                    "model_status": "success",
                    "attempt": attempt,
                    "duration_ms": round((monotonic() - started) * 1000),
                },
            )

            try:
                message = response.message
                calls = []

                for call in getattr(message, "tool_calls", None) or []:
                    function = getattr(call, "function", None)

                    if function is None:
                        continue

                    calls.append(
                        ToolCall(
                            id=str(getattr(call, "id", "") or ""),
                            name=str(getattr(function, "name", "") or ""),
                            arguments=_arguments(
                                getattr(function, "arguments", None)
                            ),
                        )
                    )

                content = _content_text(message)
            except (AttributeError, TypeError, ValueError) as error:
                raise ProviderResponseError(
                    "The provider returned an unreadable assistant message.",
                    provider_detail=str(error)[:500],
                ) from error

            if not calls and not content:
                raise ProviderResponseError(
                    "The provider returned an empty assistant message.",
                )

            return AssistantTurn(
                content=content,
                tool_calls=calls,
                tool_plan=getattr(message, "tool_plan", None),
            )

        raise ProviderResponseError("The provider returned no response.")


def default_client() -> ToolCallingClient | None:
    """The production client, or ``None`` when the agent is disabled."""

    flag = os.getenv("DATABASE_AGENT", "").strip().lower()

    if flag not in {"1", "true", "yes", "on"}:
        return None

    provider_name = (os.getenv("AI_PROVIDER") or "cohere").strip().lower()

    if provider_name == "cohere" and not os.getenv("COHERE_API_KEY"):
        logger.warning(
            "DATABASE_AGENT is enabled but COHERE_API_KEY is not set; "
            "the database agent is disabled."
        )
        return None

    from ai.providers.provider_factory import create_provider

    provider = create_provider(provider_name)

    from mcp_server.configuration import load_mcp_settings

    settings = load_mcp_settings()

    if not settings.enabled:
        return provider

    from ai.mcp_client.context import current_access_token
    from ai.mcp_client.client import MCPToolCallingClient
    from auth.mcp_credentials import MCP_CREDENTIAL_PREFIX
    from services.task_scope import current_actor

    actor = current_actor()
    token = current_access_token()

    # The /mcp endpoint authenticates only with dedicated MCP credentials.
    # REST/JWT callers keep the direct provider so their existing flows and
    # JWT authentication remain unchanged.
    if actor is not None and token and token.startswith(MCP_CREDENTIAL_PREFIX):
        return MCPToolCallingClient(
            provider,
            server_url=settings.server_url,
            access_token=token,
            actor=actor,
            cache_seconds=settings.tool_cache_seconds,
        )

    return provider
