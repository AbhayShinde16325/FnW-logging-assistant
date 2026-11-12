import json
import logging
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    HTTPException
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from database.connection import create_tables

from voice.speech_to_text import (
    transcribe_audio,
    VoiceInputError
)

from services.task_service import process_task


# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)


# Application startup
@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("Initializing application...")

    create_tables()

    logger.info("Database initialized successfully.")

    yield

    logger.info("Application shutting down.")


app = FastAPI(
    title="Task Flow Automation API",
    description="AI-powered task management using voice and text.",
    version="1.0.0",
    lifespan=lifespan
)


# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173"
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request schema
class TaskRequest(BaseModel):

    text: str
    task_id: int | None = None
    confirm_delete: bool = False
    # Staged CREATE flow (stateless echo: the draft travels with the request)
    stage: str | None = None
    draft: dict | None = None
    answers: dict | None = None
    reminder_text: str | None = None
    confirm: bool = False


# Health check
@app.get("/health")
def health_check():

    return {
        "status": "healthy",
        "message": "Task Flow Automation API is running."
    }


# TEXT INPUT ENDPOINT
@app.post("/tasks/text")
def handle_text(request: TaskRequest):

    try:

        logger.info("Received text task request.")

        result = process_task(
            user_input=request.text,
            task_id=request.task_id,
            confirm_delete=request.confirm_delete,
            stage=request.stage,
            draft=request.draft,
            answers=request.answers,
            reminder_text=request.reminder_text,
            confirm=request.confirm,
        )

        logger.info("Text task processing completed.")

        return jsonable_encoder(result)

    except ValueError as error:

        logger.warning("Text validation error: %s", error)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception("Text task processing failed.")

        raise HTTPException(
            status_code=500,
            detail="Internal task processing error."
        )


# VOICE INPUT ENDPOINT
@app.post("/tasks/voice")
def handle_voice(
    file: UploadFile = File(...),
    task_id: int | None = Form(None),
    confirm_delete: bool = Form(False),
    stage: str | None = Form(None),
    reminder_text: str | None = Form(None),
    confirm: bool = Form(False),
    state: str | None = Form(None),
):

    try:

        logger.info("Received voice input: %s", file.filename)

        # Step 1: Read uploaded audio
        audio_bytes = file.file.read()

        if not audio_bytes:
            raise HTTPException(
                status_code=400,
                detail="Uploaded audio is empty."
            )

        logger.info(
            "Audio received successfully: %s bytes",
            len(audio_bytes)
        )

        # Step 2: Speech-to-text using Faster-Whisper
        logger.info("Starting Faster-Whisper transcription...")

        transcription = transcribe_audio(
            audio_bytes,
            filename=file.filename
        )

        logger.info("Transcription completed successfully.")
        logger.info("Transcript: %s", transcription.text)
        logger.info("Detected language: %s", transcription.language)

        # Step 3: Process transcript using AI Task Service
        logger.info("Sending transcript to Task Service...")

        # Continuation state for the staged CREATE flow (voice follow-ups
        # travel as a JSON string in the `state` form field).
        state: dict = {}
        if state_json:
            try:
                state = json.loads(state_json)
            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid state JSON."
                )

        result = process_task(
            user_input=transcription.text,
            task_id=task_id,
            confirm_delete=confirm_delete,
            stage=stage or state.get("stage"),
            draft=state.get("draft"),
            answers=state.get("answers"),
            reminder_text=reminder_text or state.get("reminder_text"),
            confirm=confirm or bool(state.get("confirm")),
        )

        logger.info("Voice task processing completed.")

        # Step 4: Return response
        return {
            "transcription": transcription.text,
            "language": transcription.language,
            "confidence": transcription.language_probability,
            "duration": transcription.duration_seconds,
            "result": jsonable_encoder(result)
        }

    except HTTPException:
        raise

    except VoiceInputError as error:

        logger.warning("Voice input error: %s", error)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except ValueError as error:

        logger.warning("Validation error: %s", error)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception("Voice processing failed.")

        raise HTTPException(
            status_code=500,
            detail="Internal voice processing error."
        )

    finally:

        file.file.close()
