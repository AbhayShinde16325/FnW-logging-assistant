"""Native tool-calling client for the database agent.

The agent loop is provider-independent: it only needs an object with a
``chat(messages, tools)`` method that returns an :class:`AssistantTurn`. The
production implementation uses the existing Cohere client and its native
function/tool calling. Tests inject a small scripted client instead.

No database access happens here. The tool dispatcher in
``services.database_agent`` executes the validated tools.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MODEL_NAME = "command-a-plus-05-2026"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_plan: str | None = None

    def as_message(self) -> dict:
        message: dict[str, Any] = {"role": "assistant"}

        if self.content:
            message["content"] = self.content

        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments or {}),
                    },
                }
                for call in self.tool_calls
            ]

        if self.tool_plan:
            message["tool_plan"] = self.tool_plan

        return message


class ToolCallingClient(Protocol):
    def chat(self, *, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        ...


def _content_text(message) -> str:
    """Join the text segments of a Cohere assistant message."""

    segments = getattr(message, "content", None) or []
    parts = []

    for segment in segments:
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

    def __init__(self, client=None, model: str = MODEL_NAME):
        self._client = client
        self._model = model

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
        response = self._get_client().chat(
            model=self._model,
            messages=messages,
            tools=tools,
            temperature=0,
            thinking={"type": "disabled"},
        )

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
                    arguments=_arguments(getattr(function, "arguments", None)),
                )
            )

        return AssistantTurn(
            content=_content_text(message),
            tool_calls=calls,
            tool_plan=getattr(message, "tool_plan", None),
        )


def default_client() -> ToolCallingClient | None:
    """The production client, or ``None`` when the agent is disabled."""

    flag = os.getenv("DATABASE_AGENT", "").strip().lower()

    if flag not in {"1", "true", "yes", "on"}:
        return None

    if not os.getenv("COHERE_API_KEY"):
        logger.warning(
            "DATABASE_AGENT is enabled but COHERE_API_KEY is not set; "
            "the database agent is disabled."
        )
        return None

    return CohereToolCallingClient()
