"""Common interface implemented by every AI provider adapter."""

from __future__ import annotations

from typing import Protocol

from ai.tool_calling import AssistantTurn


class BaseAIProvider(Protocol):
    name: str

    def chat(self, *, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        """Run one model turn with the provided discovered tool schemas."""

    @property
    def message_adapter(self):
        """Return the provider message adapter used by the agent loop."""
