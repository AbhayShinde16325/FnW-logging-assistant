import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, time

from dotenv import load_dotenv

load_dotenv()

from fastapi import (
    Depends,
    FastAPI,
    UploadFile,
    File,
    Form,
    HTTPException
)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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
from services import missing_field_followup

from services.conversation_memory import update_response_feedback
from services.task_scope import TaskActor, use_task_actor
from ai.mcp_client.context import use_mcp_access_token
from mcp_server.configuration import clear_mcp_settings_cache, load_mcp_settings

import auth.config as auth_config
from auth.dependencies import require_any_permission, require_permission
from auth.permissions import (
    TASKS_CREATE,
    TASKS_DELETE,
    TASKS_READ,
    TASKS_UPDATE,
    TASK_TURN_PERMISSIONS,
)
from auth.routes import router as auth_router
from auth.employee_routes import router as employees_router
from auth.mcp_credential_routes import router as mcp_credentials_router
from auth.mcp_oauth_routes import router as mcp_oauth_router
from notification_routes import router as notifications_router
from auth.service import AuthenticatedEmployee
from database.sql_execution import confirmation_phrase, validate_confirmation
from services import task_management


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

    config_errors = auth_config.settings.secure_config_errors

    if config_errors:
        raise RuntimeError(" ".join(config_errors))

    create_tables()

    logger.info("Database initialized successfully.")

    mcp_settings = load_mcp_settings()

    if mcp_settings.enabled:
        # Reconcile after migrations/schema setup and before MCP requests are served.
        # Bootstrap credentials are a development/tunnel convenience; production
        # direct HTTPS MCP should use explicitly issued credentials.
        if mcp_settings.tunnel_enabled and not mcp_settings.is_production:
            from auth.mcp_credentials import reconcile_bootstrap_mcp_credential

            reconcile_bootstrap_mcp_credential()
        else:
            logger.info(
                "Skipping MCP bootstrap credential reconciliation "
                "(APP_ENV=%s, MCP_TUNNEL_ENABLED=%s).",
                mcp_settings.app_env,
                mcp_settings.tunnel_enabled,
            )

        from mcp_server.server import mcp_lifespan

        async with mcp_lifespan():
            logger.info(
                "MCP server started at %s (APP_ENV=%s, tunnel=%s).",
                mcp_settings.path,
                mcp_settings.app_env,
                mcp_settings.tunnel_enabled,
            )
            yield
    else:
        yield

    logger.info("Application shutting down.")


app = FastAPI(
    title="Task Flow Automation API",
    description="AI-powered task management using voice and text.",
    version="1.0.0",
    lifespan=lifespan
)

app.include_router(auth_router)
app.include_router(mcp_oauth_router)
app.include_router(mcp_credentials_router)
app.include_router(employees_router)
app.include_router(notifications_router)


clear_mcp_settings_cache()
_mcp_settings = load_mcp_settings()

if _mcp_settings.enabled:
    from mcp_server.server import mount_mcp

    mount_mcp(app)


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
    allow_credentials=True,
)


@app.middleware("http")
async def mcp_access_token_context(request, call_next):
    """Keep a caller MCP credential available to the MCP-backed adapter.

    Only dedicated ``fnw_mcp_...`` credentials are propagated. The /mcp
    endpoint rejects employee JWTs, so REST/JWT requests keep the direct agent
    path and their existing authentication remains unchanged.
    """

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()

    if scheme.strip().lower() == "bearer" and token.startswith("fnw_mcp_"):
        with use_mcp_access_token(token):
            return await call_next(request)

    return await call_next(request)


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


class FeedbackRequest(BaseModel):
    """A thumbs up (``True``) or thumbs down (``False``) rating."""

    conversation_id: str
    user_id: str
    feedback: bool


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
    # Optional notification context (from notification click / deep link).
    notification_id: int | None = None


class TaskCreateRequest(BaseModel):
    """Fields accepted when creating a task.

    Employees may only create tasks for themselves; the service replaces any
    supplied assignee with the authenticated employee.
    """

    model_config = ConfigDict(extra="forbid")

    task_title: str
    task_description: str | None = None
    assigned_to: str | None = None
    assigned_to_employee_id: int | None = None
    assigned_date: date | None = None
    assigned_time: time | None = None
    deadline_date: date | None = None
    deadline_time: time | None = None
    reminder_date: date | None = None
    reminder_time: time | None = None
    status: str | None = None
    progress: int = Field(default=0, strict=True, ge=0, le=100)


class TaskEditRequest(BaseModel):
    """Full task fields for the owner, an administrator or a manager."""

    model_config = ConfigDict(extra="forbid")

    task_title: str | None = None
    task_description: str | None = None
    assigned_to: str | None = None
    assigned_to_employee_id: int | None = None
    assigned_date: date | None = None
    assigned_time: time | None = None
    deadline_date: date | None = None
    deadline_time: time | None = None
    reminder_date: date | None = None
    reminder_time: time | None = None
    status: str | None = None
    progress: int | None = Field(default=None, strict=True, ge=0, le=100)


class TaskProgressRequest(BaseModel):
    """The progress-only task write."""

    model_config = ConfigDict(extra="forbid")

    progress: int = Field(strict=True, ge=0, le=100)


class TaskDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str | None = None


# RESPONSE FEEDBACK ENDPOINT
@app.post("/feedback")
@app.post("/tasks/feedback")
def handle_feedback(
    request: FeedbackRequest,
    employee: AuthenticatedEmployee | None = Depends(require_permission(TASKS_READ)),
):

    try:

        logger.info("Received response feedback.")

        with use_task_actor(_task_actor(employee)):
            memory = update_response_feedback(
                _memory_user_id(employee, request.user_id),
                request.conversation_id,
                request.feedback,
            )

        if memory is None:
            raise HTTPException(
                status_code=400,
                detail="A user id and conversation id are required."
            )

        return {
            "status": "success",
            "response_feedback": memory["response_feedback"],
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception("Response feedback could not be stored.")

        raise HTTPException(
            status_code=500,
            detail="Unable to store feedback."
        )


# Health check
@app.get("/health")
def health_check():

    return {
        "status": "healthy",
        "message": "Task Flow Automation API is running."
    }


def _serialize_task(task) -> dict:
    return serialize_task(task)


def _task_actor(employee: AuthenticatedEmployee | None) -> TaskActor | None:
    return TaskActor.from_employee(employee) if employee is not None else None


def _employee_name(
    employee: AuthenticatedEmployee | None,
    fallback: str | None = None,
) -> str | None:
    return employee.name if employee is not None else fallback


def _memory_user_id(
    employee: AuthenticatedEmployee | None,
    fallback: str | None = None,
) -> str | None:
    # An id, not a name or a client-generated value, owns conversation memory.
    return f"employee:{employee.id}" if employee is not None else fallback


# LIST TASKS ENDPOINT
@app.get("/tasks")
def list_tasks(
    employee: AuthenticatedEmployee | None = Depends(require_permission(TASKS_READ)),
):

    try:

        logger.info("Fetching tasks for the signed-in employee.")

        with use_task_actor(_task_actor(employee)):
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


def _task_management_error(error: task_management.TaskManagementError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=str(error),
    )


# CREATE TASK ENDPOINT
@app.post("/tasks", status_code=201)
def create_task_endpoint(
    request: TaskCreateRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_CREATE)
    ),
):

    try:

        with use_task_actor(_task_actor(employee)):
            task = task_management.create_task(
                request.model_dump(exclude_none=True)
            )

        return {
            "status": "success",
            "task": _serialize_task(task),
        }

    except task_management.TaskManagementError as error:
        raise _task_management_error(error)

    except Exception:

        logger.exception("Failed to create a task.")

        raise HTTPException(
            status_code=500,
            detail="Unable to create the task."
        )


# FULL TASK EDIT ENDPOINT (OWN TASK OR ADMIN / MANAGER)
@app.patch("/tasks/{task_id}")
def update_task_endpoint(
    task_id: int,
    request: TaskEditRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_UPDATE)
    ),
):

    values = request.model_dump(exclude_unset=True)

    try:

        with use_task_actor(_task_actor(employee)):
            task = task_management.update_task(task_id, values)

        return {
            "status": "success",
            "task": _serialize_task(task),
        }

    except task_management.TaskManagementError as error:
        raise _task_management_error(error)

    except Exception:

        logger.exception("Failed to update task %s.", task_id)

        raise HTTPException(
            status_code=500,
            detail="Unable to update the task."
        )


# EMPLOYEE PROGRESS ENDPOINT
@app.patch("/tasks/{task_id}/progress")
def update_task_progress_endpoint(
    task_id: int,
    request: TaskProgressRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_UPDATE)
    ),
):

    try:

        with use_task_actor(_task_actor(employee)):
            task = task_management.update_progress(task_id, request.progress)

        return {
            "status": "success",
            "task": _serialize_task(task),
        }

    except task_management.TaskManagementError as error:
        raise _task_management_error(error)

    except Exception:

        logger.exception("Failed to update progress for task %s.", task_id)

        raise HTTPException(
            status_code=500,
            detail="Unable to update the task progress."
        )


# DELETE TASK ENDPOINT (OWN TASK OR ADMIN / MANAGER)
@app.delete("/tasks/{task_id}")
def delete_task_endpoint(
    task_id: int,
    request: TaskDeleteRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_DELETE)
    ),
):

    if not validate_confirmation("DELETE", request.confirmation):
        raise HTTPException(
            status_code=400,
            detail=(
                "Deleting a task requires this confirmation: "
                f"{confirmation_phrase('DELETE')}"
            ),
        )

    try:

        with use_task_actor(_task_actor(employee)):
            deleted_id = task_management.delete_task(task_id)

        return {
            "status": "success",
            "deleted_task_id": deleted_id,
        }

    except task_management.TaskManagementError as error:
        raise _task_management_error(error)

    except Exception:

        logger.exception("Failed to delete task %s.", task_id)

        raise HTTPException(
            status_code=500,
            detail="Unable to delete the task."
        )


# READ ONE TASK ENDPOINT
@app.get("/tasks/{task_id}")
def read_task_by_id(
    task_id: int,
    employee: AuthenticatedEmployee | None = Depends(require_permission(TASKS_READ)),
):

    try:

        logger.info("Fetching task %s.", task_id)

        with use_task_actor(_task_actor(employee)):
            result = read_task(
                task_id,
                employee=_employee_name(employee),
            )

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
def handle_text(
    request: TaskRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_any_permission(*TASK_TURN_PERMISSIONS)
    ),
):

    try:

        logger.info("Received text task request.")

        with use_task_actor(_task_actor(employee)):
            result = process_task(
                user_input=request.text,
                task_id=request.task_id,
                confirm_delete=request.confirm_delete,
                stage=request.stage,
                draft=request.draft,
                answers=request.answers,
                reminder_text=request.reminder_text,
                confirm=request.confirm,
                employee=_employee_name(employee, request.employee),
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
class NotificationFollowupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str


@app.post("/notifications/{notification_id}/followup/respond")
def respond_notification_followup(
    notification_id: int,
    request: NotificationFollowupRequest,
    employee: AuthenticatedEmployee | None = Depends(require_permission(TASKS_READ)),
):
    actor = _task_actor(employee)
    if actor is None:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    action = (request.action or "").strip().lower()
    if action not in {"accept", "dismiss"}:
        raise HTTPException(status_code=400, detail="Action must be accept or dismiss.")
    row = missing_field_followup.respond_assignment_followup(
        notification_id,
        actor.id,
        action,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    payload = dict(getattr(row, "payload", None) or {})
    return {
        "status": "success",
        "notification_id": int(notification_id),
        "followup_status": payload.get("followup_status"),
        "task_id": int(row.task_id),
        "notification_context": payload,
    }


@app.post("/tasks/chat")
def handle_chat(
    request: ChatRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_any_permission(*TASK_TURN_PERMISSIONS)
    ),
):

    try:

        logger.info("Received chat conversation turn.")

        with use_task_actor(_task_actor(employee)):
            result = process_chat_turn(
                user_input=request.message,
                stage=request.stage,
                draft=request.draft,
                answers=request.answers,
                reminder_text=request.reminder_text,
                confirm=request.confirm,
                confirm_delete=request.confirm_delete,
                employee=_employee_name(employee, request.employee),
                conversation_id=request.conversation_id,
                user_id=_memory_user_id(employee, request.user_id),
                notification_id=request.notification_id,
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
    authenticated_employee: AuthenticatedEmployee | None = Depends(
        require_any_permission(*TASK_TURN_PERMISSIONS)
    ),
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

        with use_task_actor(_task_actor(authenticated_employee)):
            result = process_voice_turn(
                transcription.text,
                task_id=task_id,
                confirm_delete=confirm_delete,
                stage=stage or voice_state.get("stage"),
                draft=voice_state.get("draft"),
                answers=voice_state.get("answers"),
                reminder_text=reminder_text or voice_state.get("reminder_text"),
                confirm=confirm or bool(voice_state.get("confirm")),
                employee=_employee_name(
                    authenticated_employee,
                    employee or voice_state.get("employee"),
                ),
                conversation_id=(
                    conversation_id or voice_state.get("conversation_id")
                ),
                user_id=_memory_user_id(
                    authenticated_employee,
                    user_id or voice_state.get("user_id"),
                ),
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
