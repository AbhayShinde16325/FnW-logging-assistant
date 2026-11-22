"""End-to-end tests for the two-way voice conversation endpoint.

Faster-Whisper, the AI extractor and the database are all faked, so these
tests run offline and exercise only the HTTP orchestration.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as app_module
import services.task_service as task_service
from tests.test_query_session import full_extraction


class ScriptedTranscriber:
    """Return one scripted transcript per transcription call."""

    def __init__(self, transcripts):
        self.transcripts = list(transcripts)

    def __call__(self, audio_bytes, filename=None, language=None):
        if not self.transcripts:
            raise AssertionError("No scripted transcript left.")
        return SimpleNamespace(
            text=self.transcripts.pop(0),
            language="en",
            language_probability=0.9,
            duration_seconds=2.0,
        )


class FakeExtractor:
    def extract_draft(self, user_input):
        return full_extraction()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    monkeypatch.setattr(task_service, "extractor", FakeExtractor())
    monkeypatch.setattr(
        task_service,
        "create_task",
        lambda record: {
            "id": 1,
            "task_title": record.task_title,
            "task_description": record.task_description,
            "status": record.status,
            "reminder_date": (
                str(record.reminder_date) if record.reminder_date else None
            ),
            "reminder_time": (
                record.reminder_time.strftime("%H:%M")
                if record.reminder_time
                else None
            ),
        },
    )

    with TestClient(app_module.app) as test_client:
        yield test_client


def voice_turn(client, state=None):
    payload = {"file": ("note.webm", b"\x00" * 4096, "audio/webm")}
    if state is not None:
        payload["state"] = (None, json.dumps(state))
    return client.post("/tasks/voice/turn", files=payload)


def test_voice_turn_completes_two_way_flow(client):
    app_module.transcribe_audio = ScriptedTranscriber(
        ["some task", "IN_PROGRESS", "yes", "no reminder"]
    )

    response = voice_turn(client)
    assert response.status_code == 200
    body = response.json()
    assert "Here is what I have so far" in body["reply"]
    first = body["result"]
    assert first["status"] == "confirmation_required"
    assert first["missing_fields"] == []
    assert first["draft"]["task_description"] == "Summarise the API findings"

    response = voice_turn(
        client, {"stage": first["stage"], "draft": first["draft"]}
    )
    assert response.status_code == 200
    second = response.json()["result"]
    assert second["status"] == "confirmation_required"
    assert second["draft"]["status"] == "IN_PROGRESS"

    response = voice_turn(
        client, {"stage": second["stage"], "draft": second["draft"]}
    )
    assert response.status_code == 200
    reminder = response.json()["result"]
    assert reminder["status"] == "reminder_required"

    response = voice_turn(
        client, {"stage": reminder["stage"], "draft": reminder["draft"]}
    )
    assert response.status_code == 200
    final = response.json()
    assert final["result"]["status"] == "success"
    assert final["result"]["task"]["status"] == "IN_PROGRESS"
    assert final["reply"] == "Task created successfully."


def test_invalid_state_json_is_rejected(client):
    app_module.transcribe_audio = ScriptedTranscriber(["some task"])

    response = client.post(
        "/tasks/voice",
        files={"file": ("note.webm", b"\x00" * 4096, "audio/webm")},
        data={"state": "{not json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid state JSON."


def test_legacy_voice_route_still_starts_a_task(client):
    app_module.transcribe_audio = ScriptedTranscriber(["some task"])

    response = client.post(
        "/tasks/voice",
        files={"file": ("note.webm", b"\x00" * 4096, "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json()["result"]["status"] == "confirmation_required"
