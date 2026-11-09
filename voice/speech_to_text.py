from faster_whisper import WhisperModel


class SpeechToText:
    def __init__(self, model_size="small", device="cpu", compute_type="int8"):
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type
        )

    def transcribe(self, audio_path):
        try:
            segments, info = self.model.transcribe(
                audio_path,
                language=None,
                beam_size=5,
                vad_filter=True
            )

            transcription = " ".join(
                segment.text.strip() for segment in segments
            ).strip()

            if not transcription:
                raise ValueError("No speech detected in the audio.")

            return transcription

        except Exception as error:
            raise RuntimeError(
                f"Audio transcription failed: {error}"
            ) from error


if __name__ == "__main__":
    speech = SpeechToText()

    try:
        text = speech.transcribe("sample_audio.wav")
        print("Transcription:", text)
    except Exception as error:
        print(error)