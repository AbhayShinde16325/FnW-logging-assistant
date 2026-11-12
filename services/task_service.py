
from ai.query_session import (
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
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_FIELDS:
        response["status"] = "confirmation_required"
        response["message"] = result.message

    elif result.stage == STAGE_REMINDER:
        response["status"] = "reminder_required"
        response["question"] = result.question
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_REMINDER:
        response["status"] = "reminder_confirmation_required"
        response["reminder"] = result.reminder
        response["question"] = result.message
        response["message"] = result.message

    return response
