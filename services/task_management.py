"""Authorized task-management operations for the REST API.

The HTTP layer only parses requests. This module applies the role policy,
normalizes assignment through the employee foreign key and delegates all SQL
to ``database.task_repository``.
"""

from __future__ import annotations

import logging

from ai.text_extractor import sync_status_progress
from auth.permissions import (
    TASKS_CREATE,
    TASKS_DELETE,
    TASKS_READ,
    TASKS_UPDATE,
    TASKS_UPDATE_ALL,
)
from database.connection import SessionLocal
from database.models import Employee
from database.task_repository import (
    create_task as repository_create_task,
    delete_task as repository_delete_task,
    get_task,
    update_task as repository_update_task,
)
from services import notification_service
from services.task_scope import (
    current_actor,
    can_delete_task,
    can_read_task,
    can_update_all,
    can_update_task,
)


logger = logging.getLogger(__name__)

STATUS_VALUES = frozenset({"PENDING", "IN_PROGRESS", "COMPLETED"})
TASK_FIELDS = frozenset(
    {
        "task_title",
        "task_description",
        "assigned_to",
        "assigned_to_employee_id",
        "assigned_date",
        "assigned_time",
        "deadline_date",
        "deadline_time",
        "reminder_date",
        "reminder_time",
        "status",
        "progress",
    }
)


class TaskManagementError(Exception):
    status_code = 400


class TaskPermissionError(TaskManagementError):
    status_code = 403


class TaskNotFoundError(TaskManagementError):
    status_code = 404


class InvalidTaskData(TaskManagementError):
    status_code = 400


def _actor_has(permission: str, message: str) -> None:
    actor = current_actor()

    if actor is not None and not actor.has_permission(permission):
        raise TaskPermissionError(message)


def _clean_title(value) -> str:
    title = str(value or "").strip()

    if not title:
        raise InvalidTaskData("A task title is required.")

    return title


def _resolve_assignment(values: dict) -> dict:
    """Resolve an assignee id/name to the employee foreign key."""

    if "assigned_to_employee_id" not in values and "assigned_to" not in values:
        return values

    employee_id = values.get("assigned_to_employee_id")
    display_name = values.get("assigned_to")

    session = SessionLocal()

    try:
        if employee_id is not None:
            employee = session.query(Employee).filter(
                Employee.id == employee_id,
                Employee.is_active.is_(True),
            ).first()

            if employee is None:
                raise InvalidTaskData("The assignee is not an active employee.")

            values["assigned_to_employee_id"] = int(employee.id)
            values["assigned_to"] = employee.name
            return values

        name = str(display_name or "").strip()

        if not name:
            values["assigned_to_employee_id"] = None
            values["assigned_to"] = None
            return values

        employees = (
            session.query(Employee)
            .filter(
                Employee.name.ilike(name),
                Employee.is_active.is_(True),
            )
            .limit(2)
            .all()
        )

        if len(employees) != 1:
            raise InvalidTaskData(
                "The assignee must match exactly one active employee."
            )

        values["assigned_to_employee_id"] = int(employees[0].id)
        values["assigned_to"] = employees[0].name
        return values

    finally:
        session.close()


def _validate_status(value):
    if value is None:
        return None

    status = str(value).strip().upper()

    if status not in STATUS_VALUES:
        raise InvalidTaskData(
            "status must be PENDING, IN_PROGRESS, or COMPLETED."
        )

    return status


def _sync_status_progress(values: dict, task=None, *, progress_only=False) -> dict:
    if "progress" not in values and "status" not in values:
        return values

    current_status = (
        (getattr(task, "status", None) if task is not None else None) or "PENDING"
    )
    current_progress = (
        getattr(task, "progress", None) if task is not None else 0
    ) or 0

    status = _validate_status(values.get("status", current_status))
    progress = values.get("progress", current_progress)

    if isinstance(progress, bool) or not isinstance(progress, int):
        raise InvalidTaskData("progress must be an integer from 0 to 100.")

    if not 0 <= progress <= 100:
        raise InvalidTaskData("progress must be between 0 and 100.")

    if "progress" not in values and status == "COMPLETED":
        progress = 100

    if progress_only:
        status, progress = sync_status_progress(
            current_status,
            progress,
            status_changed=False,
            progress_changed=True,
        )
    else:
        status, progress = sync_status_progress(
            status,
            progress,
            status_changed="status" in values,
            progress_changed="progress" in values,
        )

    values["status"] = status
    values["progress"] = progress
    return values


def _validate_reminder_schedule(
    *,
    reminder_date,
    reminder_time,
    deadline_date,
    deadline_time,
) -> None:
    try:
        notification_service.validate_reminder_values(
            reminder_date=reminder_date,
            reminder_time=reminder_time,
            deadline_date=deadline_date,
            deadline_time=deadline_time,
        )
    except notification_service.ReminderValidationError as error:
        raise InvalidTaskData(str(error))



def _deadline_phrase(task) -> str:
    if not getattr(task, "deadline_date", None):
        return ""
    text = task.deadline_date.strftime("%d %B %Y")
    if getattr(task, "deadline_time", None):
        text += f" at {task.deadline_time.strftime('%H:%M')}"
    return text


def _notify_assignment(task, *, previous_assignee_id=None) -> None:
    assignee_id = getattr(task, "assigned_to_employee_id", None)
    if assignee_id is None:
        return
    try:
        assignee_id = int(assignee_id)
    except (TypeError, ValueError):
        return
    if previous_assignee_id is not None:
        try:
            if int(previous_assignee_id) == assignee_id:
                return
        except (TypeError, ValueError):
            pass

    title = getattr(task, "task_title", None) or f"Task #{getattr(task, 'id', '')}"
    assignee_name = (getattr(task, "assigned_to", None) or "").strip()
    greeting = f"Hey {assignee_name.split()[0].title()}, " if assignee_name else "Hey, "
    deadline = _deadline_phrase(task)
    message = f"{greeting}you have been assigned task #{task.id}: {title}."
    voice = f"{greeting}you have received a new task assignment. Your task is {title}."
    if deadline:
        message += f" Deadline: {deadline}."
        voice += f" The deadline is {deadline}. Please review it."

    try:
        notification_service.create_task_event_notification(
            int(task.id),
            assignee_id,
            notification_service.TYPE_TASK_ASSIGNED,
            title=f"New task assignment: {title}",
            message=message,
            scheduled_at=notification_service.utcnow(),
            priority=notification_service.PRIORITY_HIGH,
            voice_text=voice,
            payload={
                "notification_kind": "assignment",
                "task_title": title,
                "deadline": deadline or None,
                "employee_name": assignee_name or None,
            },
        )
    except Exception:
        logger.exception("Could not create assignment notification for task %s.", getattr(task, "id", None))


SELF_ONLY_ASSIGNMENT_MESSAGE = (
    "You can only create tasks assigned to yourself. "
    "I can create this task for you instead."
)


def _self_service_actor(actor):
    return actor is not None and not actor.has_permission(TASKS_UPDATE_ALL)


def _enforce_self_assignment(actor, *, assigned_to, assigned_to_employee_id):
    """Reject a forged assignee for an employee-created task."""

    if assigned_to_employee_id is not None:
        try:
            other_id = int(assigned_to_employee_id)
        except (TypeError, ValueError):
            raise InvalidTaskData("The assignee is not an active employee.")

        if other_id != actor.id:
            raise TaskPermissionError(SELF_ONLY_ASSIGNMENT_MESSAGE)

    name = str(assigned_to or "").strip()

    if name and name.casefold() != actor.name.casefold():
        raise TaskPermissionError(SELF_ONLY_ASSIGNMENT_MESSAGE)


def create_task(values: dict):
    _actor_has(TASKS_CREATE, "You do not have permission to create tasks.")

    actor = current_actor()
    data = {
        key: value
        for key, value in values.items()
        if key in TASK_FIELDS and value is not None
    }
    data["task_title"] = _clean_title(data.get("task_title"))
    data.setdefault("status", "PENDING")
    data.setdefault("progress", 0)

    if _self_service_actor(actor):
        _enforce_self_assignment(
            actor,
            assigned_to=data.get("assigned_to"),
            assigned_to_employee_id=data.get("assigned_to_employee_id"),
        )
        data["assigned_to"] = actor.name
        data["assigned_to_employee_id"] = actor.id
    elif actor is not None:
        data = _resolve_assignment(data)

    if actor is not None:
        data["created_by_employee_id"] = actor.id

    data = _sync_status_progress(data)
    _validate_reminder_schedule(
        reminder_date=data.get("reminder_date"),
        reminder_time=data.get("reminder_time"),
        deadline_date=data.get("deadline_date"),
        deadline_time=data.get("deadline_time"),
    )

    task = repository_create_task(data)
    _notify_assignment(task)
    return task


def get_task_for_actor(task_id: int):
    _actor_has(TASKS_READ, "You do not have permission to read tasks.")

    task = get_task(task_id)

    if task is None or not can_read_task(task):
        raise TaskNotFoundError("The task was not found.")

    return task


def update_task(task_id: int, values: dict):
    _actor_has(TASKS_UPDATE, "You do not have permission to update tasks.")

    task = get_task(task_id)

    if task is None or not can_update_task(task):
        raise TaskNotFoundError("The task was not found.")

    actor = current_actor()
    self_service = _self_service_actor(actor)
    changes_assignment = (
        "assigned_to" in values or "assigned_to_employee_id" in values
    )

    if self_service and changes_assignment:
        raise TaskPermissionError("You cannot reassign a task.")

    data = {
        key: value
        for key, value in values.items()
        if key in TASK_FIELDS and key not in {"assigned_to", "assigned_to_employee_id"}
    }

    if "task_title" in data:
        data["task_title"] = _clean_title(data["task_title"])

    if not data and not changes_assignment:
        raise InvalidTaskData("Provide at least one field to update.")

    if changes_assignment:
        data["assigned_to"] = values.get("assigned_to")
        data["assigned_to_employee_id"] = values.get("assigned_to_employee_id")
        data = _resolve_assignment(data)

    data = _sync_status_progress(data, task)
    _validate_reminder_schedule(
        reminder_date=(
            data["reminder_date"]
            if "reminder_date" in data
            else task.reminder_date
        ),
        reminder_time=(
            data["reminder_time"]
            if "reminder_time" in data
            else task.reminder_time
        ),
        deadline_date=(
            data["deadline_date"]
            if "deadline_date" in data
            else task.deadline_date
        ),
        deadline_time=(
            data["deadline_time"]
            if "deadline_time" in data
            else task.deadline_time
        ),
    )

    previous_assignee_id = getattr(task, "assigned_to_employee_id", None)
    updated = repository_update_task(task_id, data)

    if updated is None:
        raise TaskNotFoundError("The task was not found.")

    if "assigned_to_employee_id" in data or "assigned_to" in data:
        _notify_assignment(updated, previous_assignee_id=previous_assignee_id)

    return updated


def update_progress(task_id: int, progress: int):
    _actor_has(TASKS_UPDATE, "You do not have permission to update tasks.")

    task = get_task(task_id)

    if task is None or not can_update_task(task):
        raise TaskNotFoundError("The task was not found.")

    values = _sync_status_progress(
        {"progress": progress},
        task,
        progress_only=True,
    )
    updated = repository_update_task(task_id, values)

    if updated is None:
        raise TaskNotFoundError("The task was not found.")

    return updated


def delete_task(task_id: int):
    _actor_has(TASKS_DELETE, "You do not have permission to delete tasks.")

    task = get_task(task_id)

    if task is None or not can_delete_task(task):
        raise TaskNotFoundError("The task was not found.")

    notification_service.cancel_pending_notifications(
        task_id, reason="The task was deleted."
    )

    if not repository_delete_task(task_id):
        raise TaskNotFoundError("The task was not found.")

    return task_id
