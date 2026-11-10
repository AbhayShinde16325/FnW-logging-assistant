"""Voice input module: audio in, text out.

Public API for the rest of the application:

    from voice import transcribe_audio, EmptyAudioError, InvalidAudioError

    try:
        result = transcribe_audio(uploaded_bytes, filename="note.webm")
    except EmptyAudioError:
        ...  # ask the user to re-record
    except InvalidAudioError as error:
        ...  # show an unsupported/corrupt file message

    raw_text = result.text      # same type as text input -> shared AI pipeline

    except voice.TranscriptionError on engine failures.

Also available: warm_up() to preload the model at startup and
get_transcriber().settings to inspect the active model configuration.
"""

from voice.speech_to_text import (
    EmptyAudioError,
    InvalidAudioError,
    SpeechToText,
    TranscriptionError,
    TranscriptionResult,
    VoiceInputError,
    VoiceSettings,
    get_transcriber,
    transcribe_audio,
    warm_up,
)

__all__ = [
    "EmptyAudioError",
    "InvalidAudioError",
    "SpeechToText",
    "TranscriptionError",
    "TranscriptionResult",
    "VoiceInputError",
    "VoiceSettings",
    "get_transcriber",
    "transcribe_audio",
    "warm_up",
]
