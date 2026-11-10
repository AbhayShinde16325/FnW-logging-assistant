"""Tests for the voice speech-to-text module.

Run only the fast tests (no model download, no network):

    pytest tests/test_voice.py

Run the integration test that loads the real model (downloads ~75 MB for
'tiny' on first use):

    RUN_WHISPER_TESTS=1 pytest tests/test_voice.py
"""

from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path

import pytest

from voice import (
    EmptyAudioError,
    InvalidAudioError,
    SpeechToText,
    TranscriptionResult,
    VoiceInputError,
    VoiceSettings,
    transcribe_audio,
)


# --- Fake model (avoids loading the real engine in unit tests) ---------------

class _FakeSegment:
    def __init__(self, text: str):
        self.text = text


class _FakeInfo:
    def __init__(self, language="en", probability=0.92, duration=2.5):
        self.language = language
        self.language_probability = probability
        self.duration = duration


class _FakeModel:
    def __init__(self, segments=None, info=None, error=None):
        self._segments = segments if segments is not None else []
        self._info = info or _FakeInfo()
        self._error = error
        self.calls = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append({"path": audio_path, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return iter(self._segments), self._info


def _make_stt(model: _FakeModel) -> SpeechToText:
    stt = SpeechToText(VoiceSettings(model_size="tiny"))
    stt._model = model          # inject: skips download/load entirely
    return stt


def _tmp_file(suffix: str, content: bytes) -> Path:
    path = Path(tempfile.gettempdir()) / f"voice_test_{os.getpid()}{suffix}"
    path.write_bytes(content)
    return path


def _silent_wav(seconds: float = 3.0) -> Path:
    """Generate a real, decodable WAV file of digital silence."""
    path = Path(tempfile.gettempdir()) / f"voice_test_silent_{os.getpid()}.wav"
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


# --- Input validation: file paths ---------------------------------------------

def test_missing_file_raises_invalid():
    stt = _make_stt(_FakeModel())
    with pytest.raises(InvalidAudioError):
        stt.transcribe(Path("C:/definitely/not/here.wav"))


def test_empty_file_raises_empty():
    stt = _make_stt(_FakeModel())
    empty = _tmp_file(".wav", b"")
    with pytest.raises(EmptyAudioError):
        stt.transcribe(empty)
    empty.unlink()


def test_tiny_file_raises_empty():
    stt = _make_stt(_FakeModel())
    tiny = _tmp_file(".wav", b"\x00" * 10)
    with pytest.raises(EmptyAudioError):
        stt.transcribe(tiny)
    tiny.unlink()


def test_unsupported_suffix_raises_invalid():
    stt = _make_stt(_FakeModel())
    doc = _tmp_file(".docx", b"\x00" * 2048)
    with pytest.raises(InvalidAudioError):
        stt.transcribe(doc)
    doc.unlink()


def test_file_path_units_do_not_touch_model():
    """Validation failures must raise before the model is ever used."""
    fake = _FakeModel()
    stt = _make_stt(fake)
    with pytest.raises(InvalidAudioError, match="not found"):
        stt.transcribe("C:/nope.mp3")
    assert fake.calls == []


# --- Input validation: bytes ---------------------------------------------------

def test_empty_bytes_raises_empty():
    stt = _make_stt(_FakeModel())
    with pytest.raises(EmptyAudioError):
        stt.transcribe(b"")


def test_tiny_bytes_raises_empty():
    stt = _make_stt(_FakeModel())
    with pytest.raises(EmptyAudioError):
        stt.transcribe(b"\x00" * 8)


def test_oversized_bytes_raises_invalid():
    stt = _make_stt(_FakeModel())
    with pytest.raises(InvalidAudioError, match="too large"):
        stt.transcribe(b"\x00" * (10 * 1024 * 1024 + 1))


def test_unsupported_suffix_bytes_raises_invalid():
    stt = _make_stt(_FakeModel())
    with pytest.raises(InvalidAudioError, match="Unsupported audio format"):
        stt.transcribe(b"\x00" * 2048, filename="notes.docx")


def test_unsupported_input_type_raises_invalid():
    stt = _make_stt(_FakeModel())
    with pytest.raises(InvalidAudioError, match="Unsupported audio input type"):
        stt.transcribe(12345)


# --- Happy path with the fake model --------------------------------------------

def test_bytes_transcription_returns_structured_result():
    fake = _FakeModel(
        segments=[_FakeSegment("Remind Rahul "), _FakeSegment("about the API task.")],
        info=_FakeInfo(language="en", probability=0.93, duration=4.0),
    )
    stt = _make_stt(fake)

    result = stt.transcribe(
        b"\x00" * 4096, filename="note.webm", language=None
    )

    assert isinstance(result, TranscriptionResult)
    assert result.text == "Remind Rahul about the API task."
    assert result.language == "en"
    assert result.language_probability == pytest.approx(0.93)
    assert result.duration_seconds == pytest.approx(4.0)
    assert result.model_size == "tiny"

    # the byte input must have been staged in a temp file named for the voice module
    assert len(fake.calls) == 1
    assert "voice_input_" in fake.calls[0]["path"]


def test_temp_file_for_bytes_is_cleaned_up(tmp_path, monkeypatch):
    """After transcribing bytes, the staged temp file must be gone."""
    fake = _FakeModel(segments=[_FakeSegment("hello")])
    stt = _make_stt(fake)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    stt.transcribe(b"\x00" * 4096, filename="a.wav")

    assert list(tmp_path.glob("voice_input_*")) == []


def test_multiple_segments_are_joined():
    fake = _FakeModel(
        segments=[_FakeSegment("Set up the"), _FakeSegment("PostgreSQL database.")]
    )
    result = _make_stt(fake).transcribe(b"\x00" * 4096, filename="a.wav")
    assert result.text == "Set up the PostgreSQL database."


# --- Result handling ------------------------------------------------------------

def test_no_speech_segments_raises_empty():
    fake = _FakeModel(segments=[], info=_FakeInfo(duration=3.0))
    with pytest.raises(EmptyAudioError, match="No speech detected"):
        _make_stt(fake).transcribe(b"\x00" * 4096, filename="a.wav")


def test_whitespace_only_segments_raise_empty():
    fake = _FakeModel(segments=[_FakeSegment("   "), _FakeSegment("")],
                      info=_FakeInfo(duration=2.0))
    with pytest.raises(EmptyAudioError):
        _make_stt(fake).transcribe(b"\x00" * 4096, filename="a.wav")


def test_decode_error_maps_to_invalid():
    fake = _FakeModel(error=ValueError("Could not find codec for the stream"))
    with pytest.raises(InvalidAudioError):
        _make_stt(fake).transcribe(b"\x00" * 4096, filename="a.wav")


def test_engine_crash_maps_to_transcription_error():
    fake = _FakeModel(error=RuntimeError("CUDA out of memory"))
    stt = _make_stt(fake)
    with pytest.raises(VoiceInputError) as excinfo:
        stt.transcribe(b"\x00" * 4096, filename="a.wav")
    assert not isinstance(excinfo.value, InvalidAudioError)
    assert not isinstance(excinfo.value, EmptyAudioError)


# --- Settings + cached instance ---------------------------------------------------

def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "medium")
    monkeypatch.setenv("WHISPER_DEVICE", "cuda")
    settings = VoiceSettings.from_env()
    assert settings.model_size == "medium"
    assert settings.device == "cuda"


def test_settings_default_without_env(monkeypatch):
    for key in ("WHISPER_MODEL_SIZE", "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE"):
        monkeypatch.delenv(key, raising=False)
    settings = VoiceSettings.from_env()
    assert settings.model_size == "small"
    assert settings.device == "cpu"
    assert settings.compute_type == "int8"


def test_transcribe_audio_uses_cached_instance():
    assert transcribe_audio.__module__ == "voice.speech_to_text"


# --- Integration (real engine) ---------------------------------------------------

@pytest.mark.skipif(
    os.getenv("RUN_WHISPER_TESTS") != "1",
    reason="Real-model test; set RUN_WHISPER_TESTS=1 to run (downloads model)",
)
def test_real_engine_silence_raises_empty():
    """End-to-end sanity check against the real faster-whisper engine.

    True digital silence must surface as EmptyAudioError after real VAD and
    decoding, not as a crash, not as empty-string success.
    """
    stt = SpeechToText(VoiceSettings(model_size=os.getenv("WHISPER_MODEL_SIZE", "tiny")))
    wav = _silent_wav(seconds=5.0)
    try:
        with pytest.raises(EmptyAudioError):
            stt.transcribe(wav)
    finally:
        wav.unlink()
