"""Provider-agnostic JSON chat for task extraction.

`chat_json(system_prompt, user_input)` sends one chat request to the
configured provider and returns the raw text of the model's answer (the
caller parses/validates it and owns the retry loop).

Providers (config.EXTRACTION_PROVIDER):
  - "cohere"  -> Cohere command-a-plus-05-2026 via the cohere SDK (default)
  - "hermes"  -> Hermes via any OpenAI-compatible endpoint
                 (Nous Portal / OpenRouter / vLLM / Ollama)

Switching provider is one environment variable; no other code changes.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

import config


class RateLimitedError(Exception):
    """The provider returned 429; the caller may retry after a backoff."""


class ProviderError(Exception):
    """Any other provider-side failure (auth, bad request, empty reply...)."""


PROVIDER_COHERE = "cohere"
PROVIDER_HERMES = "hermes"

# Lazily-built SDK clients, cached for process lifetime.
_cohere_client = None
_openai_client = None


def active_provider() -> str:
    return (config.EXTRACTION_PROVIDER or PROVIDER_COHERE).strip().lower()


def chat_json(system_prompt: str, user_input: str) -> str:
    """Dispatch one JSON-mode chat call to the active provider."""
    provider = active_provider()

    if provider == PROVIDER_HERMES:
        return _chat_hermes(system_prompt, user_input)

    if provider == PROVIDER_COHERE:
        return _chat_cohere(system_prompt, user_input)

    raise ValueError(
        f"Unknown EXTRACTION_PROVIDER '{provider}'. "
        f"Expected '{PROVIDER_COHERE}' or '{PROVIDER_HERMES}'."
    )


# --- Cohere -------------------------------------------------------------------

def _get_cohere_client():
    global _cohere_client
    if _cohere_client is None:
        import cohere

        api_key = os.environ.get("COHERE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "COHERE_API_KEY is not set; required when "
                "EXTRACTION_PROVIDER=cohere."
            )
        _cohere_client = cohere.ClientV2(api_key=api_key)
    return _cohere_client


def _chat_cohere(system_prompt: str, user_input: str) -> str:
    client = _get_cohere_client()

    response = client.chat(
        model=config.COHERE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        response_format={"type": "json_object"},
        thinking={"type": "disabled"},
        temperature=0,
    )

    text_segments = [
        item
        for item in response.message.content
        if getattr(item, "type", None) == "text"
    ]

    if not text_segments or not text_segments[0].text:
        raise ProviderError("Cohere returned no text content.")

    return text_segments[0].text


# --- Hermes (OpenAI-compatible) -------------------------------------------------

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        import openai

        api_key = os.environ.get("HERMES_API_KEY")
        if not api_key:
            raise RuntimeError(
                "HERMES_API_KEY is not set; required when "
                "EXTRACTION_PROVIDER=hermes."
            )
        _openai_client = openai.OpenAI(
            base_url=config.HERMES_BASE_URL,
            api_key=api_key,
            timeout=120,
            max_retries=0,  # retry policy is owned by the caller
        )
    return _openai_client


def _chat_hermes(system_prompt: str, user_input: str) -> str:
    client = _get_openai_client()

    kwargs = {
        "model": config.HERMES_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    # Hermes 4 is hybrid-reasoning; keep it in direct mode for extraction.
    if config.HERMES_DISABLE_REASONING:
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as error:
        raise _map_openai_error(error) from error

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError) as error:
        raise ProviderError(f"Hermes returned an unusable response: {error}")

    if not content:
        raise ProviderError("Hermes returned no text content.")

    return content


def _map_openai_error(error: Exception) -> Exception:
    """Translate OpenAI-SDK errors into RateLimitedError / ProviderError."""
    import openai

    if isinstance(error, openai.RateLimitError):
        return RateLimitedError(str(error))

    if isinstance(error, openai.APIError):
        return ProviderError(f"Hermes API error: {error}")

    return ProviderError(f"Hermes request failed: {error}")
