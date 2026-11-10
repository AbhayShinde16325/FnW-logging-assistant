"""Speech-to-text module for the voice input path.

Accepts an audio file path or raw audio bytes, transcribes the speech with
faster-whisper and returns a structured voice.speech_to_text.TranscriptionResult.

This module is only concerned with speech-to-text. Task extraction, storage
and reminders are handled by other modules and must never be imported here.

Errors are raised as a small exception hierarchy so callers can react without
string matching:

    VoiceInputError
        InvalidAudioError    - unusable input (missing, corrupt, unsupported, too large)
        EmptyAudioError      - audio contained no detectable speech
        TranscriptionError   - the transcription engine failed unexpectedly
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel


logger = logging.getLogger(__name__)

# --- Input guards -----------------------------------------------------------

MAX_AUDIO_BYTES = 10 * 1024 * 1024      # reject recordings above 10 MiB
MIN_AUDIO_BYTES = 256                   # below this it cannot be a real recording
MAX_AUDIO_SECONDS = 120.0               # task logs are short notes, not meetings

# Silence this long means "nothing was said" without calling the model.
MIN_SPEECH_SECONDS = 0.3

# Suffixes faster-whisper (PyAV) can decode. If a filename has no suffix the
# decoder is left to probe the container, so unknown-but-valid input still works.
SUPPORTED_SUFFIXES = {
    ".wav", ".wave", ".aiff", ".aif",
    ".mp3", ".m4a", ".aac", ".wma", ".flac",
    ".ogg", ".oga", ".opus", ".webm", ".mp4", ".mkv",
}


# --- Errors -----------------------------------------------------------------

class VoiceInputError(Exception):
    """Base class for every error raised by the voice module."""


class InvalidAudioError(VoiceInputError):
    """The audio input cannot be used: missing, corrupt, unsupported or oversized."""


class EmptyAudioError(VoiceInputError):
    """The audio was accepted but contained no detectable speech."""


class TranscriptionError(VoiceInputError):
    """The transcription engine failed after a valid input was accepted."""


# --- Settings ---------------------------------------------------------------

@dataclass(frozen=True)
class VoiceSettings:
    """Model configuration, overridable per instance and via environment."""

    model_size: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    model_dir: str | None = None     # optional offline cache dir for downloads
    cpu_threads: int = 0             # 0 lets faster-whisper pick its default

    @classmethod
    def from_env(cls) -> "VoiceSettings":
        """Read WHISPER_* environment variables with safe defaults.

        The environment is read at construction time (not import time) so
        tests and the application can set it per instance.
        """
        return cls(
            model_size=os.getenv("WHISPER_MODEL_SIZE", "small"),
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            model_dir=os.getenv("WHISPER_MODEL_DIR") or None,
        )


# --- Result -----------------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionResult:
    """Structured output of the voice module.

    `text` is the integration contract: the AI task extraction module accepts
    a plain string, which is exactly what `result.text` is.
    """

    text: str
    language: str | None             # detected language, e.g. "en", "hi"
    language_probability: float      # 0.0 - 1.0
    duration_seconds: float
    model_size: str


# --- Engine -----------------------------------------------------------------

class SpeechToText:
    """Lazy, thread-safe wrapper around one faster-whisper model instance.

    The model is downloaded and loaded on first use (or on warm_up()), never
    at construction, so creating this object is always cheap.
    """

    def __init__(self, settings: VoiceSettings | None = None):
        self.settings = settings or VoiceSettings.from_env()
        self._model: WhisperModel | None = None
        self._load_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()

    # -- model lifecycle ----------------------------------------------------

    def is_ready(self) -> bool:
        """True once the model is loaded into memory."""
        return self._model is not None

    def warm_up(self) -> None:
        """Force model download/load at application startup.

        Call this once at startup so the first user request does not pay the
        cold-start cost (first run downloads ~500 MB for the 'small' model).
        """
        self._ensure_model()
        logger.info(
            "Whisper model ready: %s on %s (%s)",
            self.settings.model_size, self.settings.device, self.settings.compute_type,
        )

    def _ensure_model(self) -> WhisperModel:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    kwargs = {}
                    if self.settings.model_dir:
                        kwargs["download_root"] = self.settings.model_dir
                    if self.settings.cpu_threads:
                        kwargs["cpu_threads"] = self.settings.cpu_threads

                    logger.info("Loading Whisper model '%s' ...", self.settings.model_size)
                    self._model = WhisperModel(
                        self.settings.model_size,
                        device=self.settings.device,
                        compute_type=self.settings.compute_type,
                        **kwargs,
                    )
        return self._model

    # -- public API ----------------------------------------------------------

    def transcribe(
        self,
        audio: str | Path | bytes,
        *,
        filename: str | None = None,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio and return a TranscriptionResult.

        audio:     a file path (str or Path) or raw audio bytes
                   (e.g. an uploaded browser recording in webm/opus).
        filename:  original filename for byte input; only its extension is
                   used, to validate the format. Optional.
        language:  force a language code ("en") or None for auto-detect.

        Raises InvalidAudioError for unusable input, EmptyAudioError when no
        speech is detected and TranscriptionError for engine failures.
        """
        if isinstance(audio, (str, Path)):
            return self._transcribe_path(Path(audio), language=language)

        if isinstance(audio, (bytes, bytearray, memoryview)):
            return self._transcribe_bytes(
                bytes(audio), filename=filename, language=language
            )

        raise InvalidAudioError(
            f"Unsupported audio input type: {type(audio).__name__}. "
            "Pass a file path or raw audio bytes."
        )

    # -- private helpers ------------------------------------------------------

    def _transcribe_path(self, path: Path, *, language: str | None) -> TranscriptionResult:
        self._check_suffix(path.suffix, source=str(path))

        if not path.is_file():
            raise InvalidAudioError(f"Audio file not found: {path}")

        try:
            size = path.stat().st_size
        except OSError as error:
            raise InvalidAudioError(
                f"Audio file cannot be read: {path} ({error})"
            ) from error

        self._check_size(size, str(path))
        return self._run(path, language=language)

    def _transcribe_bytes(
        self,
        data: bytes,
        *,
        filename: str | None,
        language: str | None,
    ) -> TranscriptionResult:
        self._check_size(len(data), filename or "<bytes>")

        suffix: str | None = None
        if filename:
            suffix = Path(filename).suffix.lower()
            self._check_suffix(suffix, source=filename)

        # Windows keeps open temp files locked, so the handle is closed before
        # the model is allowed to open the file. unlink happens in `finally`.
        tmp = tempfile.NamedTemporaryFile(
            prefix="voice_input_",
            suffix=suffix or ".bin",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        try:
            with tmp:
                tmp.write(data)
            return self._run(tmp_path, language=language)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("Could not delete temp file %s", tmp_path)

    def _run(self, audio_path: Path, *, language: str | None) -> TranscriptionResult:
        model = self._ensure_model()

        # one model instance, one transcription at a time
        with self._transcribe_lock:
            try:
                segments, info = model.transcribe(
                    str(audio_path),
                    language=language,
                    beam_size=5,
                    vad_filter=True,
                    no_speech_threshold=0.6,
                    condition_on_previous_text=False,
                )

                duration = float(getattr(info, "duration", 0.0) or 0.0)
                if duration < MIN_SPEECH_SECONDS:
                    raise EmptyAudioError(
                        f"Recording is only {duration:.1f}s long. Please speak longer."
                    )
                if duration > MAX_AUDIO_SECONDS:
                    raise InvalidAudioError(
                        f"Recording is {duration:.0f}s long; the limit is "
                        f"{MAX_AUDIO_SECONDS:.0f}s."
                    )

                parts = []
                for segment in segments:            # decode + inference happen here
                    text = segment.text.strip()
                    if text:
                        parts.append(text)
            except (EmptyAudioError, InvalidAudioError):
                raise
            except (ValueError, RuntimeError) as error:
                raise self._classify_engine_error(error)

        text = " ".join(parts).strip()
        if not text:
            raise EmptyAudioError("No speech detected in the recording.")

        language_detected = getattr(info, "language", None)
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)

        logger.info(
            "Transcribed %.1fs audio: language=%s (%.2f), model=%s, %d chars",
            duration, language_detected, probability,
            self.settings.model_size, len(text),
        )

        return TranscriptionResult(
            text=text,
            language=language_detected,
            language_probability=probability,
            duration_seconds=duration,
            model_size=self.settings.model_size,
        )

    @staticmethod
    def _classify_engine_error(error: Exception) -> VoiceInputError:
        """Map raw faster-whisper/PyAV errors to the voice exception hierarchy."""
        message = str(error).lower()
        decode_markers = (
            "invalid data", "could not find codec", "error splitting",
            "moov atom", "unknown format", "no such codec",
        )
        if isinstance(error, ValueError) or any(marker in message for marker in decode_markers):
            return InvalidAudioError(f"Could not decode the audio file: {error}")
        return TranscriptionError(f"Transcription failed: {error}")

    @staticmethod
    def _check_size(size: int, source: str) -> None:
        if size == 0 or size < MIN_AUDIO_BYTES:
            raise EmptyAudioError(
                f"Audio input is empty or too small ({size} bytes): {source}"
            )
        if size > MAX_AUDIO_BYTES:
            raise InvalidAudioError(
                f"Audio input is too large ({size / 1024 / 1024:.1f} MB, "
                f"limit {MAX_AUDIO_BYTES / 1024 / 1024:.0f} MB): {source}"
            )

    @staticmethod
    def _check_suffix(suffix: str, *, source: str) -> None:
        if not suffix:
            return  # no extension: let the decoder probe the container
        if suffix not in SUPPORTED_SUFFIXES:
            raise InvalidAudioError(
                f"Unsupported audio format '{suffix}' ({source}). "
                f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
            )


# --- Module-level cached instance -------------------------------------------

_transcriber: SpeechToText | None = None
_transcriber_lock = threading.Lock()


def get_transcriber(settings: VoiceSettings | None = None) -> SpeechToText:
    """Return the shared SpeechToText instance, creating it on first call.

    Passing `settings` on a later call only applies when no instance exists
    yet; the cache then wins. Call it with settings at startup if the
    environment defaults are not what you want.
    """
    global _transcriber
    if _transcriber is None:
        with _transcriber_lock:
            if _transcriber is None:
                _transcriber = SpeechToText(settings)
    return _transcriber


def transcribe_audio(
    audio: str | Path | bytes,
    *,
    filename: str | None = None,
    language: str | None = None,
) -> TranscriptionResult:
    """One-line entry point for the rest of the application.

    from voice import transcribe_audio
    result = transcribe_audio(uploaded_bytes, filename="note.webm")
    raw_text = result.text   # -> shared AI pipeline
    """
    return get_transcriber().transcribe(audio, filename=filename, language=language)


def warm_up() -> None:
    """Load the shared model at application startup instead of first use."""
    get_transcriber().warm_up()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    audio_file = os.getenv("VOICE_SAMPLE", "sample_audio.wav")
    try:
        result = transcribe_audio(audio_file)
        print("Text:", result.text)
        print(f"Language: {result.language} ({result.language_probability:.0%})")
        print(f"Duration: {result.duration_seconds:.1f}s")
    except VoiceInputError as error:
        print(f"[{type(error).__name__}] {error}")
