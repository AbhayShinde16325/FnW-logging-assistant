import json
import logging
import os
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from database.connection import create_tables
from database.task_repository import get_all_tasks

from voice.speech_to_text import (
    transcribe_audio,
    VoiceInputError
)

from services.task_service import (
    process_task,
    process_chat_turn,
    process_voice_turn,
    read_task,
    serialize_task,
)


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


# CORS configuration. Comma-separated origins can be provided through
# CORS_ORIGINS; the local development frontend is allowed by default.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["http://localhost:5173"],
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
    # The employee talking to the assistant. When it is given, task lookups
    # are restricted to that employee and every write carries the ownership
    # condition.
    employee: str | None = None


class ChatRequest(BaseModel):
    """One turn of the live, two-way task conversation."""

    message: str
    stage: str | None = None
    draft: dict | None = None
    answers: dict | None = None
    reminder_text: str | None = None
    confirm: bool = False
    confirm_delete: bool = False
    employee: str | None = None
    # The conversation the turn belongs to. Together with the user id it
    # identifies the stored conversation memory.
    conversation_id: str | None = None
    user_id: str | None = None


# Health check
@app.get("/health")
def health_check():

    return {
        "status": "healthy",
        "message": "Task Flow Automation API is running."
    }


def _serialize_task(task) -> dict:
    return serialize_task(task)


# LIST TASKS ENDPOINT
@app.get("/tasks")
def list_tasks():

    try:

        logger.info("Fetching all tasks.")

        tasks = get_all_tasks()

        return {
            "count": len(tasks),
            "tasks": [_serialize_task(task) for task in tasks],
        }

    except Exception:

        logger.exception("Failed to fetch tasks.")

        raise HTTPException(
            status_code=500,
            detail="Unable to load tasks."
        )


# READ ONE TASK ENDPOINT
@app.get("/tasks/{task_id}")
def read_task_by_id(task_id: int):

    try:

        logger.info("Fetching task %s.", task_id)

        result = read_task(task_id)

        # A task that does not exist is answered with the same shape as a
        # task that does, so a client never hits a KeyError on "task".
        if result.get("status") == "not_found":
            return JSONResponse(status_code=404, content=jsonable_encoder(result))

        return jsonable_encoder(result)

    except ValueError as error:

        logger.warning("Task validation error: %s", error)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception("Failed to fetch task.")

        raise HTTPException(
            status_code=500,
            detail="Unable to load the task."
        )


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
            employee=request.employee,
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


# CONVERSATIONAL CHAT ENDPOINT
@app.post("/tasks/chat")
def handle_chat(request: ChatRequest):

    try:

        logger.info("Received chat conversation turn.")

        result = process_chat_turn(
            user_input=request.message,
            stage=request.stage,
            draft=request.draft,
            answers=request.answers,
            reminder_text=request.reminder_text,
            confirm=request.confirm,
            confirm_delete=request.confirm_delete,
            employee=request.employee,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
        )

        logger.info("Chat conversation turn processed.")

        return jsonable_encoder(result)

    except ValueError as error:

        logger.warning("Chat validation error: %s", error)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception("Chat turn processing failed.")

        raise HTTPException(
            status_code=500,
            detail="Internal chat processing error."
        )


def _parse_voice_state(state_json: str | None) -> dict:
    """Decode the stateless conversation state sent by the frontend."""

    if not state_json:
        return {}

    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid state JSON."
        )

    if not isinstance(state, dict):
        raise HTTPException(
            status_code=400,
            detail="State must be a JSON object."
        )

    return state


def _reply_for_result(result) -> str:
    """Human-readable reply for the next turn of the conversation."""

    if not isinstance(result, dict):
        return ""

    if result.get("reply"):
        return result["reply"]

    if result.get("status") == "success":
        return result.get("message") or "Done."

    return result.get("question") or result.get("message") or ""


# VOICE INPUT ENDPOINT (single turn + conversation continuation)
@app.post("/tasks/voice")
@app.post("/tasks/voice/turn")
def handle_voice(
    file: UploadFile = File(...),
    task_id: int | None = Form(None),
    confirm_delete: bool = Form(False),
    stage: str | None = Form(None),
    reminder_text: str | None = Form(None),
    confirm: bool = Form(False),
    state: str | None = Form(None),
    employee: str | None = Form(None),
    conversation_id: str | None = Form(None),
    user_id: str | None = Form(None),
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

        # Step 3: Restore the stateless conversation state, then process the
        # transcript with the AI task engine.
        logger.info("Sending transcript to Task Service...")

        voice_state = _parse_voice_state(state)

        result = process_voice_turn(
            transcription.text,
            task_id=task_id,
            confirm_delete=confirm_delete,
            stage=stage or voice_state.get("stage"),
            draft=voice_state.get("draft"),
            answers=voice_state.get("answers"),
            reminder_text=reminder_text or voice_state.get("reminder_text"),
            confirm=confirm or bool(voice_state.get("confirm")),
            employee=employee or voice_state.get("employee"),
            conversation_id=conversation_id or voice_state.get("conversation_id"),
            user_id=user_id or voice_state.get("user_id"),
        )

        logger.info("Voice task processing completed.")

        encoded_result = jsonable_encoder(result)

        # Step 4: Return transcript + AI reply so the frontend can continue
        # the two-way conversation.
        return {
            "transcription": transcription.text,
            "language": transcription.language,
            "confidence": transcription.language_probability,
            "duration": transcription.duration_seconds,
            "reply": _reply_for_result(encoded_result),
            "result": encoded_result
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
