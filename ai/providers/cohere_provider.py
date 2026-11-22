"""Cohere adapter for the provider-independent agent loop."""

from __future__ import annotations

from ai.tool_calling import AssistantTurn, CohereToolCallingClient


class CohereProvider:
    name = "cohere"

    def __init__(self, client: CohereToolCallingClient | None = None, **kwargs):
        self.client = client or CohereToolCallingClient(**kwargs)

    @property
    def message_adapter(self):
        return self.client.message_adapter

    def chat(self, *, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        return self.client.chat(messages=messages, tools=tools)
