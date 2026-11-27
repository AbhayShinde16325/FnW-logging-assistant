"""Missing-field follow-up using existing notifications + conversation memory.

No new DB tables. Follow-up state is stored in notification payload and
conversation memory context. All DB writes go through existing services.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database.connection import SessionLocal
from database.models import Employee, Notification, Task
from services import notification_service

logger = logging.getLogger(__name__)

TYPE_TASK_MISSING_DETAILS = getattr(notification_service, "TYPE_TASK_MISSING_DETAILS", "TASK_MISSING_DETAILS")
STATUS_PENDING = "pending"
STATUS_COLLECTING = "collecting"
STATUS_COMPLETED = "completed"
STATUS_DISMISSED = "dismissed"

# Business-rule-driven required fields for newly assigned meeting/task follow-up.
# Only ask for fields that are genuinely missing and actionable.
DEFAULT_FOLLOWUP_FIELDS = ("deadline_date", "deadline_time")


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get(task, name, default=None):
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def detect_missing_fields(task: Task | dict[str, Any]) -> list[str]:
    """Return missing actionable fields for assignment follow-up.

    Description is optional for task validity but should be offered for
    completion when absent. Progress/status defaults (0/PENDING) are not missing.
    """
    missing: list[str] = []
    description = str(_get(task, "task_description") or "").strip()
    if not description:
        missing.append("task_description")

    dd = _get(task, "deadline_date")
    dt = _get(task, "deadline_time")
    if dd is None and dt is None:
        missing.extend(["deadline_date", "deadline_time"])
    elif dd is None:
        missing.append("deadline_date")
    elif dt is None:
        missing.append("deadline_time")

    progress = _get(task, "progress")
    if progress is None:
        missing.append("progress")

    # Title/assignee/creator are identity fields; never prompt employee to change.
    return missing


def missing_field_labels(fields: list[str]) -> list[str]:
    labels = {
        "task_description": "Task description",
        "deadline_date": "Deadline date",
        "deadline_time": "Deadline time",
        "progress": "Progress",
        "assigned_date": "Assigned date",
        "assigned_time": "Assigned time",
    }
    return [labels.get(f, f.replace("_", " ").title()) for f in fields]


def _deterministic_scheduled_at(task: Task | dict[str, Any]) -> datetime:
    """Deterministic timestamp for idempotent notification creation."""
    ad = _get(task, "assigned_date")
    at = _get(task, "assigned_time")
    if ad is not None and at is not None:
        return datetime.combine(ad, at)
    if ad is not None:
        return datetime.combine(ad, datetime.min.time())
    created = _get(task, "created_at")
    if isinstance(created, datetime):
        return created.replace(tzinfo=None)
    return utcnow_naive().replace(microsecond=0)


def find_active_followup_notification(session, *, employee_id: int, task_id: int) -> Notification | None:
    return (
        session.query(Notification)
        .filter(
            Notification.employee_id == int(employee_id),
            Notification.task_id == int(task_id),
            Notification.notification_type == TYPE_TASK_MISSING_DETAILS,
            Notification.delivery_status != notification_service.STATUS_CANCELLED,
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .first()
    )


def get_followup_state(notification: Notification | dict | None) -> str:
    payload = {}
    if notification is None:
        return ""
    if isinstance(notification, dict):
        payload = notification.get("payload") or {}
    else:
        payload = getattr(notification, "payload", None) or {}
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("followup_status") or "").strip().lower()


def create_missing_details_notification(task_id: int, *, source: str = "meeting_sync", meeting_id: str | None = None, action_item_id: str | None = None) -> Notification | None:
    session = SessionLocal()
    try:
        task = session.query(Task).filter(Task.id == int(task_id)).first()
        if task is None:
            return None
        employee_id = _get(task, "assigned_to_employee_id")
        if employee_id is None:
            return None
        employee = session.query(Employee).filter(Employee.id == int(employee_id), Employee.is_active.is_(True)).first()
        if employee is None:
            return None

        missing = detect_missing_fields(task)
        if not missing:
            return None

        existing = find_active_followup_notification(session, employee_id=int(employee.id), task_id=int(task.id))
        if existing is not None:
            state = get_followup_state(existing)
            if state in {STATUS_DISMISSED, STATUS_COMPLETED}:
                return None
            # Pending/collecting/unknown: don't duplicate.
            return existing

        scheduled_at = _deterministic_scheduled_at(task)
        title_name = (task.task_title or f"Task #{task.id}").strip()
        labels = missing_field_labels(missing)
        missing_text = ", ".join(labels)
        greeting = (employee.name or "").strip().split()[0] if (employee.name or "").strip() else "there"
        title = f"Complete task details: {title_name}"[:255]
        message = (
            f"Hey {greeting}! You have received a new task: {title_name}. "
            f"Some details are missing: {missing_text}. Would you like me to help you complete these details?"
        )
        voice = f"Hey {greeting}, your new task {title_name} is missing {missing_text}. Would you like to complete these details now?"
        payload = {
            "kind": "task_missing_details_followup",
            "followup_status": STATUS_PENDING,
            "task_id": int(task.id),
            "employee_id": int(employee.id),
            "meeting_id": str(meeting_id) if meeting_id else None,
            "action_item_id": str(action_item_id) if action_item_id else None,
            "missing_fields": missing,
            "missing_field_labels": labels,
            "source": source,
            "actions": ["accept", "dismiss"],
        }

        # Reuse existing notification service (idempotent by unique key).
        row = notification_service.create_task_event_notification(
            int(task.id),
            int(employee.id),
            TYPE_TASK_MISSING_DETAILS,
            title=title,
            message=message,
            scheduled_at=scheduled_at,
            priority=notification_service.PRIORITY_HIGH,
            voice_text=voice,
            payload=payload,
        )
        if row is None:
            # May already exist due to unique key race; fetch it.
            row = find_active_followup_notification(session, employee_id=int(employee.id), task_id=int(task.id))
        if row is not None:
            # Patch destination URL with notification id for deep link context.
            p = dict(getattr(row, "payload", None) or {})
            p.setdefault("followup_status", STATUS_PENDING)
            p["destination_url"] = f"/?task={int(task.id)}&notification={int(row.id)}&followup=1"
            row.payload = p
            session.add(row)
            session.commit()
            session.refresh(row)
        return row
    except Exception:
        session.rollback()
        logger.exception("Could not create missing-details follow-up notification.")
        return None
    finally:
        session.close()


def mark_followup(notification_id: int, employee_id: int, status: str) -> Notification | None:
    status = (status or "").strip().lower()
    if status not in {STATUS_PENDING, STATUS_COLLECTING, STATUS_COMPLETED, STATUS_DISMISSED}:
        raise ValueError("Invalid follow-up status.")
    session = SessionLocal()
    try:
        row = (
            session.query(Notification)
            .filter(Notification.id == int(notification_id), Notification.employee_id == int(employee_id))
            .first()
        )
        if row is None:
            return None
        payload = dict(row.payload or {})
        is_followup = (
            row.notification_type == TYPE_TASK_MISSING_DETAILS
            or payload.get("kind") in {"task_missing_details_followup", "task_assignment"}
            or bool(payload.get("has_missing_fields"))
        )
        if not is_followup:
            return None
        payload["followup_status"] = status
        payload["followup_updated_at"] = utcnow_naive().isoformat() + "Z"
        row.payload = payload
        if status == STATUS_DISMISSED:
            row.is_read = True
        session.commit()
        session.refresh(row)
        return row
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_notification_for_employee(notification_id: int, employee_id: int) -> Notification | None:
    session = SessionLocal()
    try:
        return (
            session.query(Notification)
            .filter(Notification.id == int(notification_id), Notification.employee_id == int(employee_id))
            .first()
        )
    finally:
        session.close()


def respond_assignment_followup(notification_id: int, employee_id: int, action: str) -> Notification | None:
    """Handle Yes/No for assignment or missing-details follow-up notifications.

    Uses existing notification payload for state; no DB schema changes.
    """
    status = STATUS_COLLECTING if action == "accept" else STATUS_DISMISSED
    session = SessionLocal()
    try:
        row = (
            session.query(Notification)
            .filter(Notification.id == int(notification_id), Notification.employee_id == int(employee_id))
            .first()
        )
        if row is None:
            return None
        payload = dict(row.payload or {})
        is_followup = (
            row.notification_type == TYPE_TASK_MISSING_DETAILS
            or payload.get("kind") in {"task_missing_details_followup", "task_assignment"}
            or bool(payload.get("has_missing_fields"))
        )
        if not is_followup:
            return None

        task = session.query(Task).filter(Task.id == int(row.task_id)).first()
        if task is None or int(getattr(task, "assigned_to_employee_id", 0) or 0) != int(employee_id):
            return None

        missing = detect_missing_fields(task)
        payload["missing_fields"] = missing
        payload["missing_field_labels"] = missing_field_labels(missing)
        payload["has_missing_fields"] = bool(missing)
        payload["followup_status"] = STATUS_COMPLETED if not missing else status
        payload["followup_updated_at"] = utcnow_naive().isoformat() + "Z"
        payload["destination_url"] = f"/?task={int(row.task_id)}&notification={int(row.id)}&followup=1"
        payload["context_source"] = payload.get("context_source") or "task_assignment_notification"
        row.payload = payload
        if action == "dismiss":
            row.is_read = True
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
