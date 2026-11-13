"""Tests for ai/llm_client.py and the provider seam in ai/text_extractor.py.

No real API calls: SDK clients are faked, and the retry clock is patched.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

import ai.llm_client as llm_client
import ai.text_extractor as text_extractor
from ai.text_extractor import ExtractionResult, TaskExtractor, TaskDetails


# --- fakes -----------------------------------------------------------------------

class FakeCohereText:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeCohereResponse:
    def __init__(self, text):
        self.message = type("M", (), {"content": [FakeCohereText(text)]})()


class FakeCohereClient:
    def __init__(self, text='{"task_title": "x"}'):
        self.text = text
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return FakeCohereResponse(self.text)


class FakeOpenAICompletions:
    def __init__(self, content='{"task_title": "x"}'):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)

        if isinstance(self.content, Exception):
            raise self.content

        message = type("M", (), {"content": self.content})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self, content='{"task_title": "x"}'):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeOpenAICompletions(content)


def _api_error(status: int):
    """Build a real OpenAI SDK error object without network access."""
    from openai import APIStatusError

    request = httpx.Request("POST", "https://hermes.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return APIStatusError("boom", response=response, body=None)


@pytest.fixture()
def fresh_clients():
    """Reset the lazy client caches around each test."""
    llm_client._cohere_client = None
    llm_client._openai_client = None
    yield
    llm_client._cohere_client = None
    llm_client._openai_client = None


# --- provider dispatch ---------------------------------------------------------------

def test_default_provider_is_cohere(monkeypatch):
    monkeypatch.delenv("EXTRACTION_PROVIDER", raising=False)
    import importlib
    import config
    importlib.reload(config)

    assert llm_client.active_provider() == "cohere"


def test_unknown_provider_rejected(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "chatgpt")

    with pytest.raises(ValueError, match="Unknown EXTRACTION_PROVIDER"):
        llm_client.chat_json("sys", "user")


def test_cohere_path_returns_text(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "cohere")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    fake = FakeCohereClient('{"task_title": "Cohere task"}')
    monkeypatch.setattr(llm_client, "_get_cohere_client", lambda: fake)

    result = llm_client.chat_json("sys", "user input")

    assert result == '{"task_title": "Cohere task"}'
    assert fake.calls[0]["model"] == llm_client.config.COHERE_MODEL
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert fake.calls[0]["thinking"] == {"type": "disabled"}


def test_cohere_missing_api_key_is_friendly(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "cohere")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
        llm_client.chat_json("sys", "user")


def test_hermes_path_returns_text(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    fake = FakeOpenAIClient('{"task_title": "Hermes task"}')
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda: fake)

    result = llm_client.chat_json("sys", "user input")

    assert result == '{"task_title": "Hermes task"}'
    call = fake.chat.completions.calls[0]
    assert call["model"] == llm_client.config.HERMES_MODEL
    assert call["response_format"] == {"type": "json_object"}
    assert call["temperature"] == 0
    # reasoning kept in direct mode for extraction
    assert call["extra_body"] == {"reasoning": {"enabled": False}}


def test_hermes_reasoning_can_be_kept_on(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    monkeypatch.setattr(llm_client.config, "HERMES_DISABLE_REASONING", False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    fake = FakeOpenAIClient()
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda: fake)

    llm_client.chat_json("sys", "user input")

    assert "extra_body" not in fake.chat.completions.calls[0]


def test_hermes_missing_api_key_is_friendly(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="HERMES_API_KEY"):
        llm_client.chat_json("sys", "user")


def test_hermes_empty_content_maps_to_provider_error(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    fake = FakeOpenAIClient(content=None)
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda: fake)

    with pytest.raises(llm_client.ProviderError, match="no text content"):
        llm_client.chat_json("sys", "user")


def test_hermes_rate_limit_maps_to_rate_limited(monkeypatch, fresh_clients):
    from openai import RateLimitError

    request = httpx.Request("POST", "https://hermes.test/v1/chat/completions")
    response = httpx.Response(429, request=request)
    fake = FakeOpenAIClient(content=RateLimitError("slow down", response=response, body=None))

    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda: fake)

    with pytest.raises(llm_client.RateLimitedError):
        llm_client.chat_json("sys", "user")


def test_hermes_api_error_maps_to_provider_error(monkeypatch, fresh_clients):
    monkeypatch.setattr(llm_client.config, "EXTRACTION_PROVIDER", "hermes")
    fake = FakeOpenAIClient(content=_api_error(400))
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda: fake)

    with pytest.raises(llm_client.ProviderError, match="Hermes"):
        llm_client.chat_json("sys", "user")


# --- the extraction seam --------------------------------------------------------------

def test_extract_task_uses_chat_json(monkeypatch):
    payload = json.dumps(
        {
            "task_title": "Python API research",
            "assigned_to": "Rahul",
            "assigned_date": "2026-11-10",
            "assigned_time": "10:30",
            "deadline_date": "2026-11-15",
            "deadline_time": "17:00",
            "status": "IN_PROGRESS",
            "progress": 50,
        }
    )
    seen = {}

    def fake_chat_json(system_prompt, user_input):
        seen["system"] = system_prompt
        seen["user"] = user_input
        return payload

    monkeypatch.setattr(text_extractor, "chat_json", fake_chat_json)

    task = TaskExtractor().extract_task("Rahul does research")

    assert isinstance(task, TaskDetails)
    assert task.task_title == "Python API research"
    assert task.assigned_date == date(2026, 11, 10)
    assert "extraction assistant" in seen["system"]


def test_extract_draft_uses_chat_json(monkeypatch):
    payload = json.dumps(
        {
            "task_title": "Testing",
            "assigned_when": "10 November 2026 at 10:30 AM",
            "deadline_when": "15 November 2026 at 5 PM",
            "reminder_when": "30 minutes before the deadline",
        }
    )
    monkeypatch.setattr(text_extractor, "chat_json", lambda s, u: payload)

    result = TaskExtractor().extract_draft("Rahul does research")

    assert isinstance(result, ExtractionResult)
    assert result.deadline_when == "15 November 2026 at 5 PM"


def test_invalid_json_is_retried_then_raises(monkeypatch):
    calls = []

    def fake_chat_json(system_prompt, user_input):
        calls.append(1)
        return "not json at all"

    monkeypatch.setattr(text_extractor, "chat_json", fake_chat_json)

    with pytest.raises(RuntimeError, match="Invalid response"):
        TaskExtractor().extract_task("Rahul does research")

    assert len(calls) == text_extractor.MAX_ATTEMPTS


def test_rate_limit_retries_with_backoff(monkeypatch):
    calls = []
    slept = []

    def fake_chat_json(system_prompt, user_input):
        calls.append(1)
        if len(calls) == 1:
            raise text_extractor.RateLimitedError("429")
        return json.dumps({"task_title": "Recovered task"})

    monkeypatch.setattr(text_extractor, "chat_json", fake_chat_json)
    monkeypatch.setattr(text_extractor, "sleep", lambda seconds: slept.append(seconds))

    task = TaskExtractor().extract_task("Rahul does research")

    assert task.task_title == "Recovered task"
    assert len(calls) == 2
    assert slept == [60]


def test_provider_error_surfaces_as_runtime_error(monkeypatch):
    def fake_chat_json(system_prompt, user_input):
        raise text_extractor.ProviderError("auth failed")

    monkeypatch.setattr(text_extractor, "chat_json", fake_chat_json)

    with pytest.raises(RuntimeError, match="Extraction provider error"):
        TaskExtractor().extract_task("Rahul does research")
