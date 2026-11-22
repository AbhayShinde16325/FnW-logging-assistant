"""Select the configured AI provider without leaking it into business code."""

from __future__ import annotations

import os


def provider_name() -> str:
    return (os.getenv("AI_PROVIDER") or "cohere").strip().lower()


def create_provider(name: str | None = None):
    selected = (name or provider_name()).strip().lower()

    if selected == "cohere":
        from ai.providers.cohere_provider import CohereProvider

        return CohereProvider()

    if selected == "openai":
        raise RuntimeError(
            "The OpenAI provider adapter is not implemented yet. The MCP "
            "client and tool schemas are provider-independent; add "
            "ai/providers/openai_provider.py when enabling it."
        )

    raise RuntimeError(f"Unsupported AI_PROVIDER: {selected}")
