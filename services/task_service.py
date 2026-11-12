
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
    confirm_delete: bool = False
):
    if not user_input or not user_input.strip():
        return {"status": "error", "message": "Input cannot be empty."}

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

    # CREATE / UPDATE
    task = extractor.extract_task(user_input)

    # Reject input that does not contain a task
    if not task or not task.task_title or not task.task_title.strip():
        return {
            "status": "no_task_detected",
            "message": "No valid task detected. Please describe an actual task."
        }

    # CREATE
    if operation == "CREATE":
        saved_task = create_task(task)

        return {
            "status": "success",
            "message": "Task created successfully.",
            "task": saved_task
        }

    # UPDATE
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
