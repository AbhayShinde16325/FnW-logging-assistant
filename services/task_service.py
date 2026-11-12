
from ai.query_session import (
    FIELD_LABELS,
    FIELD_ORDER,
    STAGE_CONFIRM_FIELDS,
    STAGE_CONFIRM_REMINDER,
    STAGE_DONE,
    STAGE_FIELDS,
    STAGE_REMINDER,
    TaskDraftEngine,
)
from ai.text_extractor import TaskExtractor
from database.task_repository import (
    create_task,
    update_task,
    delete_task,
    get_all_tasks
)

extractor = TaskExtractor()


def detect_operation(user_input: str) -> str:
    text = user_input.lower()

    if any(word in text for word in ["delete", "remove", "erase", "cancel task"]):
        return "DELETE"

    if any(word in text for word in ["update", "modify", "change", "revise",
                                     "increase progress", "decrease progress",
                                     "mark as completed"]):
        return "UPDATE"

    return "CREATE"


def process_task(
    user_input: str,
    task_id: int | None = None,
    confirm_delete: bool = False,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
):
    is_start = stage in (None, "", "start")

    if is_start and (not user_input or not user_input.strip()):
        return {"status": "error", "message": "Input cannot be empty."}

    # Continuation of the staged CREATE flow.
    if not is_start:
        return _run_create_flow(
            user_input,
            stage=stage,
            draft=draft,
            answers=answers,
            reminder_text=reminder_text,
            confirm=confirm,
        )

    operation = detect_operation(user_input)

    # DELETE
    if operation == "DELETE":
        if task_id is None:
            return {
                "status": "task_selection_required",
                "tasks": get_all_tasks()
            }

        if not confirm_delete:
            return {
                "status": "confirmation_required",
                "task_id": task_id
            }

        if not delete_task(task_id):
            return {"status": "error", "message": "Task not found."}

        return {"status": "success", "message": "Task deleted successfully."}

    # CREATE uses the staged draft flow (clarify -> confirm -> reminder -> save).
    if operation == "CREATE":
        return _run_create_flow(
            user_input,
            stage="start",
            draft=None,
            answers=None,
            reminder_text=None,
            confirm=False,
        )

    # UPDATE: single-shot extraction (unchanged behavior).
    task = extractor.extract_task(user_input)

    # Reject input that does not contain a task
    if not task or not task.task_title or not task.task_title.strip():
        return {
            "status": "no_task_detected",
            "message": "No valid task detected. Please describe an actual task."
        }

    if task_id is None:
        return {
            "status": "task_selection_required",
            "tasks": get_all_tasks()
        }

    updated_task = update_task(
        task_id,
        task.model_dump(exclude_none=True)
    )

    if not updated_task:
        return {"status": "error", "message": "Task not found."}

    return {
        "status": "success",
        "message": "Task updated successfully.",
        "task": updated_task
    }


AFFIRMATIVE_PHRASES = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "correct", "confirmed",
    "confirm", "sure", "save", "save it", "looks good", "proceed",
    "continue", "go ahead", "haan", "han", "theek hai", "thik hai",
    "sahi hai", "sahi", "ha",
}

NEGATIVE_PHRASES = {
    "no", "nope", "nah", "not", "wrong", "incorrect", "change", "edit",
    "cancel", "stop", "redo", "nahi", "nahin", "galat",
}


def _normalize_voice_text(text: str) -> str:
    return " ".join(
        (text or "").lower().replace(".", " ").replace(",", " ").split()
    )


def _is_affirmation(text: str) -> bool:
    normalized = _normalize_voice_text(text)

    if normalized in AFFIRMATIVE_PHRASES:
        return True

    words = set(normalized.split())
    if words & NEGATIVE_PHRASES:
        return False

    return any(
        normalized == phrase or normalized.startswith(f"{phrase} ")
        for phrase in ("yes", "yeah", "ok", "okay", "confirm", "correct", "haan")
    )


def _is_negative(text: str) -> bool:
    normalized = _normalize_voice_text(text)

    if normalized in NEGATIVE_PHRASES:
        return True

    return bool(set(normalized.split()) & NEGATIVE_PHRASES)


def _display_value(name: str, value) -> str:
    if value is None or value == "":
        return "-"

    if name.endswith("_date"):
        try:
            from datetime import date

            return date.fromisoformat(str(value)).strftime("%d %B %Y")
        except ValueError:
            return str(value)

    if name.endswith("_time"):
        return str(value)

    return str(value)


def _draft_summary(draft: dict) -> str:
    lines = []
    for name in FIELD_ORDER:
        label = FIELD_LABELS.get(name, name)
        lines.append(f"• {label}: {_display_value(name, (draft or {}).get(name))}")
    return "\n".join(lines)


def process_chat_turn(
    user_input: str,
    *,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
):
    """Handle one turn of the live chat conversation.

    The assistant asks for exactly one missing field at a time. On the next
    turn the user's natural-language reply is interpreted, merged into the
    existing draft JSON, and the next missing field is asked for.
    """

    transcript = (user_input or "").strip()
    draft = draft or {}

    # Backwards compatibility with the older staged CREATE flow.
    if stage == "clarify":
        stage = STAGE_FIELDS

    if stage in (None, "", "start"):
        if not transcript:
            return {
                "status": "error",
                "message": "Input cannot be empty.",
                "reply": "Tell me about the task you want to create.",
            }

        return _run_create_flow(
            transcript,
            stage="start",
            draft=None,
            answers=None,
            reminder_text=None,
            confirm=False,
        )

    if not transcript and stage not in (
        STAGE_CONFIRM_FIELDS,
        STAGE_CONFIRM_REMINDER,
    ):
        return {
            "status": "error",
            "message": "Input cannot be empty.",
            "reply": "Please send a reply so I can continue.",
        }

    engine = TaskDraftEngine(extractor)

    try:
        if stage == STAGE_FIELDS:
            result = engine.answer(draft, transcript)

        elif stage == STAGE_CONFIRM_FIELDS:
            if answers:
                result = engine.apply(draft, answers)
            elif confirm or _is_affirmation(transcript):
                result = engine.confirm_fields(draft, True)
            elif _is_negative(transcript):
                return {
                    "status": "confirmation_required",
                    "stage": STAGE_CONFIRM_FIELDS,
                    "draft": draft,
                    "message": (
                        "Okay. Tell me which field to change and its new "
                        "value, for example 'progress is 80 percent'."
                    ),
                    "reply": (
                        "Okay. Tell me which field to change and its new "
                        "value, for example 'progress is 80 percent'."
                    ),
                }
            else:
                result = engine.edit(draft, transcript)

        elif stage == STAGE_REMINDER:
            result = engine.set_reminder(
                draft, reminder_text or transcript
            )

        elif stage == STAGE_CONFIRM_REMINDER:
            if confirm or _is_affirmation(transcript):
                result = engine.confirm_reminder(draft, True)
            elif _is_negative(transcript):
                result = engine.confirm_reminder(draft, False)
            else:
                result = engine.set_reminder(draft, transcript)

        else:
            return {
                "status": "error",
                "message": f"Unknown stage '{stage}'.",
                "reply": f"Unknown stage '{stage}'.",
            }

    except (RuntimeError, ValueError) as error:
        return {
            "status": "error",
            "message": str(error),
            "reply": str(error),
        }

    return _engine_result_to_response(result)


def process_voice_turn(
    user_input: str,
    *,
    task_id: int | None = None,
    confirm_delete: bool = False,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
):
    """Drive one conversation turn from a spoken transcript.

    The transcript is treated like the user's reply to the assistant's last
    question, so voice and text share the same one-field-at-a-time flow.
    """

    transcript = (user_input or "").strip()

    if stage in (None, "", "start"):
        return process_task(
            transcript,
            task_id=task_id,
            confirm_delete=confirm_delete,
            stage="start",
        )

    return process_chat_turn(
        transcript,
        stage=stage,
        draft=draft,
        answers=answers,
        reminder_text=reminder_text,
        confirm=confirm,
    )


def _run_create_flow(
    user_input: str,
    *,
    stage: str | None,
    draft: dict | None,
    answers: dict | None,
    reminder_text: str | None,
    confirm: bool,
):
    engine = TaskDraftEngine(extractor)

    try:
        if stage in (None, "", "start"):
            result = engine.start(user_input)
        elif stage == "clarify":
            result = engine.apply(draft or {}, answers or {})
        elif stage == "confirm_fields":
            if answers:
                result = engine.apply(draft or {}, answers)
            else:
                result = engine.confirm_fields(draft or {}, bool(confirm))
        elif stage == "reminder":
            result = engine.set_reminder(draft or {}, reminder_text or "")
        elif stage == "confirm_reminder":
            if reminder_text:
                result = engine.set_reminder(draft or {}, reminder_text)
            else:
                result = engine.confirm_reminder(draft or {}, bool(confirm))
        else:
            return {"status": "error", "message": f"Unknown stage '{stage}'."}

    except RuntimeError as error:
        # Cohere/API failures surface as a normal error result.
        return {"status": "error", "message": str(error)}

    return _engine_result_to_response(result)


def _engine_result_to_response(result):
    if result.done:
        saved_task = create_task(result.record)

        return {
            "status": "success",
            "message": "Task created successfully.",
            "reply": "Task created successfully.",
            "task": saved_task
        }

    response = {
        "stage": result.stage,
        "draft": result.draft,
    }

    if result.stage == STAGE_FIELDS:
        response["status"] = "clarification_required"
        response["missing_fields"] = result.missing_fields
        response["question"] = result.question
        response["reply"] = result.question or result.message or ""
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_FIELDS:
        response["status"] = "confirmation_required"
        response["message"] = result.message
        response["reply"] = (
            "Here is what I have so far:\n"
            f"{_draft_summary(result.draft)}\n\n"
            "Reply 'yes' to save the task, or tell me which field to change."
        )

    elif result.stage == STAGE_REMINDER:
        response["status"] = "reminder_required"
        response["question"] = result.question
        response["reply"] = result.question or result.message or ""
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_REMINDER:
        response["status"] = "reminder_confirmation_required"
        response["reminder"] = result.reminder
        response["question"] = result.message
        response["message"] = result.message
        response["reply"] = result.message or ""

    return response
