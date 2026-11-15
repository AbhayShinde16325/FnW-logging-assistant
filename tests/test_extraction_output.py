"""Tests for the structured output of the extraction model.

The model answers with JSON. A value of the wrong type (a task id that is
not a number) used to break the whole turn: the JSON was invalid, the
extraction failed and the next message was answered as a new task.

The extraction now asks for a structured answer (the JSON schema of the
Pydantic model) and hands every candidate of the answer to the Pydantic
model, which accepts the answer or rejects it. An invalid task id is never
converted into another value and never into another action.

No Cohere call is needed: the model client is replaced by a fake one.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ai.text_extractor as extractor_module
from ai.text_extractor import (
    ExtractionResult,
    TaskUpdate,
    json_schema_for,
    parse_llm_json,
    parse_llm_json_candidates,
    repair_unquoted_values,
    validate_model_output,
)


class FakeText:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeMessage:
    def __init__(self, text: str):
        self.content = [FakeText(text)]


class FakeResponse:
    def __init__(self, text: str):
        self.message = FakeMessage(text)


class FakeClient:
    """A model client that answers with prepared text.

    The last answer is repeated when the list is empty: the extractor asks
    the model a second time when the first answer was not valid.
    """

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls: list[dict] = []
        self.last = None

    def chat(self, **kwargs):
        self.calls.append(kwargs)

        if self.answers:
            self.last = self.answers.pop(0)

        return FakeResponse(self.last if self.last is not None else "{}")


@pytest.fixture()
def fake_client(monkeypatch):
    client = FakeClient()

    monkeypatch.setattr(extractor_module, "client", client)

    return client


# --- the structured output ---------------------------------------------------------


def test_the_schema_declares_the_task_id_as_a_number():
    schema = json_schema_for(TaskUpdate)

    task_id = schema["$defs"]["TaskUpdateFind"]["properties"]["task_id"]

    assert "integer" in json.dumps(task_id)
    assert "null" in json.dumps(task_id)


def test_the_schema_only_uses_supported_keywords():
    schema = json_schema_for(TaskUpdate)

    def keywords(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    # The keys of these objects are names, not keywords.
                    for sub in value.values():
                        yield from keywords(sub)
                    continue

                yield key
                yield from keywords(value)
        elif isinstance(node, list):
            for item in node:
                yield from keywords(item)

    used = set(keywords(schema))

    # The bounds of the Pydantic model are not part of the schema: the
    # endpoint does not accept them.
    assert "minimum" not in used
    assert "maximum" not in used
    assert used <= extractor_module.SCHEMA_KEYWORDS


def test_the_schema_requires_the_properties():
    schema = json_schema_for(TaskUpdate)

    assert schema["required"]
    assert schema["$defs"]["TaskUpdateFind"]["required"]
    assert schema["$defs"]["TaskUpdateChanges"]["required"]


def test_the_model_is_asked_for_the_structured_output(fake_client):
    fake_client.answers.append(
        json.dumps(
            {
                "find": {"task_id": 211, "task_title": None, "assigned_to": None},
                "changes": {"progress": 10},
            }
        )
    )

    result = extractor_module.TaskExtractor().extract_update(
        "Update task 211 to 10 percent"
    )

    assert result.find.task_id == 211

    response_format = fake_client.calls[0]["response_format"]

    assert response_format["type"] == "json_object"
    assert response_format["json_schema"]["$defs"]["TaskUpdateFind"]


# --- the validation of the answer ---------------------------------------------------


def test_an_unquoted_value_is_quoted():
    text = '{"find": {"task_id": 22004d, "task_title": null}}'

    assert "22004d" in repair_unquoted_values(text)
    assert '"22004d"' in repair_unquoted_values(text)


def test_the_keywords_of_json_are_not_quoted():
    text = '{"a": true, "b": false, "c": null, "d": 12}'

    assert repair_unquoted_values(text) == text


def test_an_invalid_task_id_is_rejected_by_the_model():
    raw = '{"find": {"task_id": 22004d, "task_title": null, "assigned_to": null}, "changes": {}}'

    with pytest.raises(ValidationError):
        validate_model_output(TaskUpdate, raw)


def test_an_invalid_task_id_is_never_converted():
    raw = '{"find": {"task_id": "22004d", "task_title": null, "assigned_to": null}, "changes": {}}'

    with pytest.raises(ValidationError):
        validate_model_output(TaskUpdate, raw)


def test_a_valid_task_id_is_accepted():
    raw = '{"find": {"task_id": 211, "task_title": null, "assigned_to": null}, "changes": {"progress": 10}}'

    result = validate_model_output(TaskUpdate, raw)

    assert result.find.task_id == 211
    assert result.changes.progress == 10


def test_a_numeric_task_id_is_accepted():
    raw = '{"find": {"task_id": "211"}, "changes": {}}'

    assert validate_model_output(TaskUpdate, raw).find.task_id == 211


def test_the_answer_is_found_after_the_reasoning_of_the_model():
    raw = (
        "1. **Analyze the request:** the employee wants a progress update.\n"
        '   Example: {"find": {"task_id": 1}}\n'
        "2. **Answer:**\n"
        '{"find": {"task_id": 211, "task_title": null, "assigned_to": null}, '
        '"changes": {"progress": 10}}'
    )

    result = validate_model_output(TaskUpdate, raw)

    assert result.find.task_id == 211
    assert result.changes.progress == 10


def test_the_candidates_are_returned_from_the_last_one():
    raw = '{"a": 1} {"b": 2}'

    assert parse_llm_json_candidates(raw) == [{"b": 2}, {"a": 1}]


def test_the_first_object_is_parsed():
    raw = '{"find": {"task_id": 211}} {"other": true}'

    assert parse_llm_json(raw) == {"find": {"task_id": 211}}


def test_a_broken_answer_raises_a_json_error():
    with pytest.raises(json.JSONDecodeError):
        parse_llm_json("there is no json in this answer")


# --- the extractor ------------------------------------------------------------------


def test_a_malformed_answer_never_becomes_another_task(fake_client):
    """The malformed answer of the model ends the turn, it does not change it."""

    broken = (
        "1. **Analyze:**\n"
        '{"find": {"task_id": 22004d, "task_title": null, "assigned_to": null}, '
        '"changes": {"progress": 10}}'
    )

    fake_client.answers.append(broken)

    with pytest.raises(RuntimeError):
        extractor_module.TaskExtractor().extract_update(
            "Update the progress of the revenue audit 22004d to 10%"
        )


def test_the_extraction_uses_the_schema_of_its_model(fake_client):
    fake_client.answers.append(
        json.dumps(
            {
                "task_title": None,
                "assigned_to": None,
                "assigned_when": None,
                "deadline_when": "21 November 2026",
                "reminder_when": None,
                "status": None,
                "progress": None,
            }
        )
    )

    result = extractor_module.TaskExtractor().extract_draft(
        "Also update its deadline to 21 November 2026"
    )

    assert result.deadline_when == "21 November 2026"

    schema = fake_client.calls[0]["response_format"]["json_schema"]

    assert schema["properties"]["deadline_when"]


def test_the_extraction_of_a_draft_rejects_an_invalid_value(fake_client):
    fake_client.answers.append(
        '{"task_title": null, "assigned_to": null, "assigned_when": null, '
        '"deadline_when": null, "reminder_when": null, "status": null, '
        '"progress": "ten percent"}'
    )

    with pytest.raises(RuntimeError):
        extractor_module.TaskExtractor().extract_draft("update the progress")


def test_the_extraction_result_is_validated_by_the_model():
    result = ExtractionResult.model_validate(
        {
            "task_title": "Business report",
            "assigned_to": None,
            "assigned_when": None,
            "deadline_when": "21 November 2026",
            "reminder_when": None,
            "status": None,
            "progress": None,
        }
    )

    assert result.deadline_when == "21 November 2026"
