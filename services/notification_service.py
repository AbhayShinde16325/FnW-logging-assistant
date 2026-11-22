"""Persistent notifications and the reminder delivery worker.

Task reminders are stored as local wall-clock date/time values on ``tasks``.
This module converts them to UTC at scheduling time, creates one notification
per employee/task/type/scheduled instant, and delivers pending notifications
through Web Push without holding a database transaction during network I/O.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from database.connection import SessionLocal
from database.models import Employee, Notification, PushSubscription, Task
from services import push_service

logger = logging.getLogger(__name__)

UTC = timezone.utc

NOTIFICATION_TYPE = "TASK_REMINDER"
TYPE_TASK_REMINDER = "TASK_REMINDER"
TYPE_DEADLINE_APPROACHING = "DEADLINE_APPROACHING"
TYPE_TASK_OVERDUE = "TASK_OVERDUE"
TYPE_TASK_ASSIGNED = "TASK_ASSIGNED"
TYPE_TASK_COMPLETED = "TASK_COMPLETED"

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_RETRY = "RETRY"
STATUS_SENT = "SENT"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"
STATUS_NO_SUBSCRIPTION = "NO_SUBSCRIPTION"
STATUS_CANCELLED = "CANCELLED"

UNSENT_STATUSES = (STATUS_PENDING, STATUS_RETRY, STATUS_PROCESSING)
OPEN_TASK_STATUSES = ("PENDING", "IN_PROGRESS")

DEFAULT_TIMEZONE = "Asia/Kolkata"


class ReminderValidationError(ValueError):
    """A reminder date/time combination is incomplete or invalid."""


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value

    return value.astimezone(UTC).replace(tzinfo=None)


def valid_timezone(name: str | None) -> str:
    candidate = (name or "").strip() or DEFAULT_TIMEZONE

    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise ReminderValidationError(
            "Use a valid IANA timezone, for example Asia/Kolkata."
        )

    return candidate


def employee_timezone(employee: Employee | None) -> str:
    try:
        return valid_timezone(getattr(employee, "timezone", None))
    except ReminderValidationError:
        return DEFAULT_TIMEZONE


def reminder_utc(
    reminder_date: date | None,
    reminder_time: time | None,
    timezone_name: str | None,
) -> datetime | None:
    if reminder_date is None or reminder_time is None:
        return None

    tzinfo = ZoneInfo(valid_timezone(timezone_name))
    local = datetime.combine(reminder_date, reminder_time).replace(tzinfo=tzinfo)

    return local.astimezone(UTC).replace(tzinfo=None)


def deadline_utc(
    deadline_date: date | None,
    deadline_time: time | None,
    timezone_name: str | None,
) -> datetime | None:
    return reminder_utc(deadline_date, deadline_time, timezone_name)


def validate_reminder_values(
    *,
    reminder_date: date | None,
    reminder_time: time | None,
    deadline_date: date | None,
    deadline_time: time | None,
) -> None:
    """Validate the existing reminder fields without inventing a value."""

    if (reminder_date is None) != (reminder_time is None):
        raise ReminderValidationError(
            "A reminder needs both a date and a time (or neither)."
        )

    if reminder_date is None or reminder_time is None:
        return

    if deadline_date is not None and deadline_time is not None:
        reminder = datetime.combine(reminder_date, reminder_time)
        deadline = datetime.combine(deadline_date, deadline_time)

        if reminder > deadline:
            raise ReminderValidationError(
                "The reminder must be at or before the task deadline."
            )


def get_employee_timezone(employee_id: int) -> str | None:
    session = SessionLocal()

    try:
        row = session.query(Employee.timezone).filter(
            Employee.id == employee_id
        ).first()

        return employee_timezone_by_value(row[0] if row else None)
    finally:
        session.close()


def employee_timezone_by_value(value: str | None) -> str:
    try:
        return valid_timezone(value)
    except ReminderValidationError:
        return DEFAULT_TIMEZONE


def set_employee_timezone(employee_id: int, name: str) -> str:
    timezone_name = valid_timezone(name)
    session = SessionLocal()

    try:
        employee = session.query(Employee).filter(
            Employee.id == employee_id
        ).first()

        if employee is None:
            raise ReminderValidationError("The employee was not found.")

        employee.timezone = timezone_name
        employee.updated_at = utcnow()
        session.commit()

        return timezone_name
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def cancel_pending_notifications(
    task_id: int,
    *,
    reason: str = "The task reminder changed before delivery.",
) -> int:
    """Cancel unsent notifications for one task.

    The row is kept for audit/history, but it can never be delivered or
    retried after cancellation.
    """

    session = SessionLocal()

    try:
        count = (
            session.query(Notification)
            .filter(
                Notification.task_id == task_id,
                Notification.delivery_status.in_(UNSENT_STATUSES),
                Notification.delivered_at.is_(None),
            )
            .update(
                {
                    Notification.delivery_status: STATUS_CANCELLED,
                    Notification.last_error: reason,
                    Notification.next_retry_at: None,
                    Notification.locked_at: None,
                },
                synchronize_session=False,
            )
        )
        session.commit()

        return int(count or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


PRIORITY_LOW = "LOW"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_CRITICAL = "CRITICAL"
PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_CRITICAL)
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}


def normalize_priority(value: str | None, default: str = PRIORITY_NORMAL) -> str:
    candidate = str(value or default).strip().upper()
    return candidate if candidate in PRIORITY_RANK else default


def _notification_meta(row) -> dict:
    payload = getattr(row, "payload", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    meta = dict(payload)
    priority = normalize_priority(getattr(row, "priority", None) or meta.get("priority"))
    meta["priority"] = priority
    voice = getattr(row, "voice_text", None) or meta.get("voice_text")
    if voice:
        meta["voice_text"] = str(voice)
    return meta


def serialize_notification(row: Notification) -> dict:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() + "Z" if value is not None else None

    meta = _notification_meta(row)
    destination_url = meta.get("destination_url") or f"/?task={int(row.task_id)}&notification={int(row.id)}"

    return {
        "id": int(row.id),
        "employee_id": int(row.employee_id),
        "task_id": int(row.task_id),
        "title": row.title,
        "message": row.message,
        "notification_type": row.notification_type,
        "priority": meta["priority"],
        "voice_text": meta.get("voice_text"),
        "is_read": bool(row.is_read),
        "scheduled_at": iso(row.scheduled_at),
        "created_at": iso(row.created_at),
        "delivered_at": iso(row.delivered_at),
        "delivery_status": row.delivery_status,
        "retry_count": int(row.retry_count or 0),
        "last_error": row.last_error,
        "destination_url": destination_url,
        "payload": meta,
    }


def list_notifications(
    employee_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> tuple[list[Notification], int]:
    session = SessionLocal()

    try:
        query = session.query(Notification).filter(
            Notification.employee_id == employee_id,
            or_(
                Notification.delivery_status.is_(None),
                Notification.delivery_status != STATUS_CANCELLED,
            ),
        )

        if unread_only:
            query = query.filter(Notification.is_read.is_(False))

        total = query.count()
        rows = (
            query.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset(max(offset, 0))
            .limit(max(1, min(limit, 200)))
            .all()
        )

        return rows, int(total)
    finally:
        session.close()


def unread_notification_count(employee_id: int) -> int:
    session = SessionLocal()

    try:
        return int(
            session.query(Notification)
            .filter(
                Notification.employee_id == employee_id,
                Notification.is_read.is_(False),
                or_(
                    Notification.delivery_status.is_(None),
                    Notification.delivery_status != STATUS_CANCELLED,
                ),
            )
            .count()
        )
    finally:
        session.close()


def mark_notification_read(employee_id: int, notification_id: int) -> bool:
    session = SessionLocal()

    try:
        row = session.query(Notification).filter(
            Notification.id == notification_id,
            Notification.employee_id == employee_id,
        ).first()

        if row is None:
            return False

        if not row.is_read:
            row.is_read = True
            session.commit()

        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_all_notifications_read(employee_id: int) -> int:
    session = SessionLocal()

    try:
        count = (
            session.query(Notification)
            .filter(
                Notification.employee_id == employee_id,
                Notification.is_read.is_(False),
                or_(
                    Notification.delivery_status.is_(None),
                    Notification.delivery_status != STATUS_CANCELLED,
                ),
            )
            .update({Notification.is_read: True}, synchronize_session=False)
        )
        session.commit()

        return int(count or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- device subscriptions ---------------------------------------------------


def validate_subscription(endpoint: str, p256dh: str, auth: str) -> dict:
    endpoint = (endpoint or "").strip()
    p256dh = (p256dh or "").strip()
    auth_key = (auth or "").strip()

    if not endpoint or not endpoint.startswith("https://"):
        raise ReminderValidationError(
            "A valid HTTPS push subscription endpoint is required."
        )

    if len(endpoint) > 1024:
        raise ReminderValidationError("The push endpoint is too long.")

    if not p256dh or not auth_key:
        raise ReminderValidationError(
            "The push subscription keys are incomplete."
        )

    if len(p256dh) > 512 or len(auth_key) > 512:
        raise ReminderValidationError("The push subscription keys are too long.")

    return {
        "endpoint": endpoint,
        "p256dh_key": p256dh,
        "auth_key": auth_key,
    }


def register_push_subscription(
    employee_id: int,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    device_name: str | None = None,
) -> PushSubscription:
    values = validate_subscription(endpoint, p256dh, auth)
    now = utcnow()
    session = SessionLocal()

    try:
        existing = session.query(PushSubscription).filter(
            PushSubscription.endpoint == values["endpoint"]
        ).first()

        if existing is not None and int(existing.employee_id) != int(employee_id):
            raise PermissionError(
                "This browser subscription belongs to another employee."
            )

        if existing is None:
            row = PushSubscription(
                employee_id=employee_id,
                endpoint=values["endpoint"],
                p256dh_key=values["p256dh_key"],
                auth_key=values["auth_key"],
                device_name=(device_name or "").strip()[:255] or None,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row = existing
            row.p256dh_key = values["p256dh_key"]
            row.auth_key = values["auth_key"]
            row.device_name = (
                (device_name or "").strip()[:255] or row.device_name
            )
            row.is_active = True
            row.updated_at = now

        session.commit()
        session.refresh(row)

        return row
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def unregister_push_subscription(employee_id: int, endpoint: str) -> bool:
    endpoint = (endpoint or "").strip()
    session = SessionLocal()

    try:
        row = session.query(PushSubscription).filter(
            PushSubscription.employee_id == employee_id,
            PushSubscription.endpoint == endpoint,
        ).first()

        if row is None:
            return False

        row.is_active = False
        row.updated_at = utcnow()
        session.commit()

        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def list_push_subscriptions(employee_id: int) -> list[PushSubscription]:
    session = SessionLocal()

    try:
        return (
            session.query(PushSubscription)
            .filter(
                PushSubscription.employee_id == employee_id,
                PushSubscription.is_active.is_(True),
            )
            .order_by(PushSubscription.updated_at.desc())
            .all()
        )
    finally:
        session.close()


def subscription_payload(row: PushSubscription) -> dict:
    return {
        "id": int(row.id),
        "endpoint": row.endpoint,
        "device_name": row.device_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_used_at": row.last_used_at,
    }




def _event_notification_values(
    *,
    employee: Employee,
    task: Task,
    notification_type: str,
    title: str,
    message: str,
    scheduled_at: datetime,
    priority: str = PRIORITY_NORMAL,
    voice_text: str | None = None,
    payload: dict | None = None,
    created_at: datetime | None = None,
) -> dict:
    now = created_at or utcnow()
    meta = dict(payload or {})
    meta.setdefault("priority", normalize_priority(priority))
    if voice_text:
        meta.setdefault("voice_text", voice_text)
    meta.setdefault("destination_url", f"/?task={int(task.id)}")
    voice = voice_text or meta.get("voice_text")
    return {
        "employee_id": int(employee.id),
        "task_id": int(task.id),
        "title": title[:255],
        "message": message,
        "notification_type": notification_type,
        "priority": normalize_priority(priority),
        "voice_text": str(voice)[:1000] if voice else None,
        "payload": meta,
        "is_read": False,
        "scheduled_at": scheduled_at,
        "created_at": now,
        "delivery_status": STATUS_PENDING,
        "retry_count": 0,
        "next_retry_at": now,
    }


def create_task_event_notification(
    task_id: int,
    employee_id: int,
    notification_type: str,
    *,
    title: str,
    message: str,
    scheduled_at: datetime | None = None,
    priority: str = PRIORITY_NORMAL,
    voice_text: str | None = None,
    payload: dict | None = None,
) -> Notification | None:
    """Create one employee-scoped task event notification.

    Idempotent via the unique (employee, task, type, scheduled_at) key.
    """

    session = SessionLocal()
    try:
        task = session.query(Task).filter(Task.id == int(task_id)).first()
        employee = session.query(Employee).filter(Employee.id == int(employee_id)).first()
        if task is None or employee is None or not employee.is_active:
            return None

        when = _as_utc_naive(scheduled_at or utcnow())
        values = _event_notification_values(
            employee=employee,
            task=task,
            notification_type=notification_type,
            title=title,
            message=message,
            scheduled_at=when,
            priority=priority,
            voice_text=voice_text,
            payload=payload,
            created_at=utcnow(),
        )

        existing = session.query(Notification.id).filter(
            Notification.employee_id == values["employee_id"],
            Notification.task_id == values["task_id"],
            Notification.notification_type == values["notification_type"],
            Notification.scheduled_at == values["scheduled_at"],
        ).first()
        if existing is not None:
            return None

        row = Notification(**values)
        session.add(row)
        try:
            session.commit()
            session.refresh(row)
            return row
        except IntegrityError:
            session.rollback()
            return None
    except Exception:
        session.rollback()
        logger.exception("Could not create task event notification.")
        return None
    finally:
        session.close()


def _due_bucket(now: datetime, minutes: int = 30) -> datetime:
    stamp = _as_utc_naive(now)
    minute = (stamp.minute // minutes) * minutes
    return stamp.replace(minute=minute, second=0, microsecond=0)


# --- worker -----------------------------------------------------------------


@dataclass(frozen=True)
class ReminderWorkerConfig:
    poll_seconds: int = 20
    batch_size: int = 100
    max_retries: int = 5
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    max_age_hours: int = 168
    processing_timeout_seconds: int = 300
    frontend_url: str = "http://localhost:8080"
    proactive_alerts: bool = True

    @classmethod
    def from_env(cls) -> "ReminderWorkerConfig":
        def integer(name: str, default: int) -> int:
            try:
                return max(1, int(os.getenv(name, str(default))))
            except (TypeError, ValueError):
                return default

        def flag(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            poll_seconds=integer("REMINDER_POLL_SECONDS", 20),
            batch_size=integer("REMINDER_BATCH_SIZE", 100),
            max_retries=integer("REMINDER_MAX_RETRIES", 5),
            retry_base_seconds=integer("REMINDER_RETRY_BASE_SECONDS", 30),
            retry_max_seconds=integer("REMINDER_RETRY_MAX_SECONDS", 3600),
            max_age_hours=integer("REMINDER_MAX_AGE_HOURS", 168),
            processing_timeout_seconds=integer(
                "REMINDER_PROCESSING_TIMEOUT_SECONDS", 300
            ),
            frontend_url=os.getenv(
                "FRONTEND_URL", "http://localhost:8080"
            ).rstrip("/"),
            proactive_alerts=flag("REMINDER_PROACTIVE_ALERTS", True),
        )


@dataclass(frozen=True)
class _DeliveryContext:
    notification_id: int
    employee_id: int
    task_id: int
    task_title: str
    task_status: str
    deadline_date: date | None
    deadline_time: time | None
    timezone_name: str
    title: str
    message: str
    notification_type: str
    priority: str
    voice_text: str | None
    payload: dict
    retry_count: int
    subscriptions: tuple[dict, ...]


def _deadline_text(
    deadline_date: date | None,
    deadline_time: time | None,
    timezone_name: str,
) -> str | None:
    if deadline_date is None:
        return None

    text = deadline_date.strftime("%d %B %Y")
    if deadline_time is not None:
        text += " at " + deadline_time.strftime("%H:%M")

    return text


def _deadline_dt(task: Task, timezone_name: str):
    if task.deadline_date is None:
        return None
    return deadline_utc(task.deadline_date, task.deadline_time, timezone_name)


def _reminder_priority(task: Task, scheduled_at: datetime, timezone_name: str) -> str:
    deadline = _deadline_dt(task, timezone_name)
    if deadline is not None and scheduled_at >= deadline:
        return PRIORITY_CRITICAL
    if deadline is not None and (deadline - scheduled_at) <= timedelta(hours=1):
        return PRIORITY_HIGH
    return PRIORITY_NORMAL


def _time_remaining_text(now: datetime, deadline: datetime | None) -> str | None:
    if deadline is None:
        return None
    delta = deadline - now
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "the deadline has passed"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} remaining"
    hours = minutes // 60
    rem = minutes % 60
    if hours < 24:
        if rem:
            return f"{hours} hour{'s' if hours != 1 else ''} and {rem} minute{'s' if rem != 1 else ''} remaining"
        return f"{hours} hour{'s' if hours != 1 else ''} remaining"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} remaining"


def _first_name(employee: Employee) -> str:
    name = str(getattr(employee, "name", "") or "").strip()
    if not name:
        return "there"
    return name.split()[0].strip().title() or "there"


def _spoken_task_title(title: str | None) -> str:
    text = str(title or "your task").strip()
    return text or "your task"


def _notification_text(
    task: Task,
    employee: Employee,
    scheduled_at: datetime | None = None,
) -> tuple[str, str, str, str, dict]:
    timezone_name = employee_timezone(employee)
    deadline = _deadline_text(task.deadline_date, task.deadline_time, timezone_name)
    now = _as_utc_naive(scheduled_at or utcnow())
    deadline_at = _deadline_dt(task, timezone_name)
    remaining = _time_remaining_text(now, deadline_at)
    priority = _reminder_priority(task, now, timezone_name)
    name = _first_name(employee)
    task_title = _spoken_task_title(getattr(task, "task_title", None))

    if priority == PRIORITY_CRITICAL and remaining == "the deadline has passed":
        title = f"Overdue: {task.task_title}"
        message = f"Hey {name}, task #{task.id} \"{task_title}\" is overdue."
        voice = (
            f"Hey {name}, this is an urgent reminder. "
            f"Your task {task_title} is overdue. "
            f"Please review it now and update your progress."
        )
    elif priority == PRIORITY_HIGH and remaining:
        title = f"Deadline soon: {task.task_title}"
        message = (
            f"Hey {name}, task #{task.id} \"{task_title}\" is due soon. "
            f"{remaining.capitalize()}."
        )
        voice = (
            f"Hey {name}, this is an important reminder for you. "
            f"Your task {task_title} is due soon. "
            f"You have {remaining}. Please complete it on time."
        )
    else:
        title = f"Task reminder: {task.task_title}"
        message = f"Hey {name}, reminder for task #{task.id}: \"{task_title}\"."
        if remaining and remaining != "the deadline has passed":
            voice = (
                f"Hey {name}, this is a reminder for you. "
                f"Your task is {task_title}. "
                f"You have {remaining}. Please take care of it on time."
            )
        else:
            voice = (
                f"Hey {name}, this is a reminder for you. "
                f"Your task is {task_title}. "
                f"Please review it and make progress when you can."
            )

    if deadline:
        message += f" Deadline: {deadline}."

    payload = {
        "priority": priority,
        "voice_text": voice,
        "task_title": task.task_title,
        "employee_name": getattr(employee, "name", None),
        "deadline": deadline,
        "time_remaining": remaining,
        "notification_kind": "reminder",
    }
    return title[:255], message, priority, voice, payload


class ReminderWorker:
    """One polling loop's worth of reminder processing.

    ``clock``, ``session_factory`` and ``push_sender`` are injectable so the
    complete flow can be tested deterministically and without a browser.
    """

    def __init__(
        self,
        *,
        config: ReminderWorkerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        session_factory=None,
        push_sender: Callable[[dict, dict], None] | None = None,
    ) -> None:
        self.config = config or ReminderWorkerConfig.from_env()
        self.clock = clock or utcnow
        self.session_factory = session_factory or SessionLocal
        self.push_sender = push_sender or push_service.send_web_push

    def now(self) -> datetime:
        return _as_utc_naive(self.clock())

    def run_once(self) -> dict:
        now = self.now()

        stats = {
            "recovered": 0,
            "created": 0,
            "claimed": 0,
            "sent": 0,
            "retry": 0,
            "failed": 0,
            "cancelled": 0,
            "no_subscription": 0,
        }

        try:
            stats["recovered"] = self.recover_stale_processing(now)
        except Exception:
            logger.exception("Could not recover stale reminder deliveries.")

        try:
            stats["created"] = self.create_due_notifications(now)
        except Exception:
            logger.exception("Could not create due reminder notifications.")

        try:
            claimed = self.claim_pending_notifications(now)
        except Exception:
            logger.exception("Could not claim pending reminder notifications.")
            claimed = []

        stats["claimed"] = len(claimed)

        for notification_id in claimed:
            try:
                result = self.deliver_notification(notification_id)
            except Exception:
                logger.exception(
                    "Reminder notification %s failed unexpectedly.",
                    notification_id,
                )
                result = self._record_unexpected_failure(notification_id, now)

            if result in stats:
                stats[result] += 1

        return stats

    def recover_stale_processing(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.config.processing_timeout_seconds)
        session = self.session_factory()

        try:
            count = (
                session.query(Notification)
                .filter(
                    Notification.delivery_status == STATUS_PROCESSING,
                    Notification.locked_at.is_not(None),
                    Notification.locked_at < cutoff,
                )
                .update(
                    {
                        Notification.delivery_status: STATUS_RETRY,
                        Notification.locked_at: None,
                        Notification.next_retry_at: now,
                        Notification.last_error: (
                            "Recovered after the worker stopped mid-delivery."
                        ),
                    },
                    synchronize_session=False,
                )
            )
            session.commit()

            return int(count or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_due_notifications(self, now: datetime) -> int:
        session = self.session_factory()

        try:
            today = now.date()
            lower_date = today - timedelta(
                days=(self.config.max_age_hours // 24) + 2
            )
            candidates = (
                session.query(Task, Employee)
                .join(Employee, Task.assigned_to_employee_id == Employee.id)
                .filter(
                    Employee.is_active.is_(True),
                    Task.assigned_to_employee_id.is_not(None),
                    Task.reminder_date.is_not(None),
                    Task.reminder_time.is_not(None),
                    Task.reminder_date >= lower_date,
                    Task.reminder_date <= today + timedelta(days=2),
                    Task.status.in_(OPEN_TASK_STATUSES),
                )
                .order_by(Task.reminder_date, Task.reminder_time, Task.id)
                .limit(max(self.config.batch_size * 5, 500))
                .all()
            )

            created = 0
            max_age = timedelta(hours=self.config.max_age_hours)

            for task, employee in candidates:
                timezone_name = employee_timezone(employee)

                try:
                    scheduled_at = reminder_utc(
                        task.reminder_date,
                        task.reminder_time,
                        timezone_name,
                    )
                except ReminderValidationError:
                    logger.warning(
                        "Task %s has an invalid reminder timezone.", task.id
                    )
                    continue

                if scheduled_at is None or scheduled_at > now:
                    continue

                if now - scheduled_at > max_age:
                    continue

                title, message, priority, voice, payload = _notification_text(task, employee, scheduled_at)
                payload["destination_url"] = f"/?task={int(task.id)}"
                values = {
                    "employee_id": int(employee.id),
                    "task_id": int(task.id),
                    "title": title,
                    "message": message,
                    "notification_type": NOTIFICATION_TYPE,
                    "priority": priority,
                    "voice_text": voice,
                    "payload": payload,
                    "is_read": False,
                    "scheduled_at": scheduled_at,
                    "created_at": now,
                    "delivery_status": STATUS_PENDING,
                    "retry_count": 0,
                    "next_retry_at": now,
                }

                created += self._insert_notification(session, values)

            # Deadline-aware proactive alerts (idempotent by scheduled bucket).
            if self.config.proactive_alerts:
                created += self._create_deadline_alerts(session, now)

            session.commit()
            return created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_deadline_alerts(self, session, now: datetime) -> int:
        """Create overdue and approaching-deadline notifications.

        Uses fixed UTC buckets as scheduled_at so duplicates are prevented by
        the unique key and the same alert is not emitted every poll.
        """

        created = 0
        today = now.date()
        rows = (
            session.query(Task, Employee)
            .join(Employee, Task.assigned_to_employee_id == Employee.id)
            .filter(
                Employee.is_active.is_(True),
                Task.assigned_to_employee_id.is_not(None),
                Task.deadline_date.is_not(None),
                Task.status.in_(OPEN_TASK_STATUSES),
                Task.deadline_date >= today - timedelta(days=30),
                Task.deadline_date <= today + timedelta(days=2),
            )
            .order_by(Task.deadline_date, Task.deadline_time, Task.id)
            .limit(max(self.config.batch_size * 5, 500))
            .all()
        )

        for task, employee in rows:
            timezone_name = employee_timezone(employee)
            deadline_at = _deadline_dt(task, timezone_name)
            if deadline_at is None:
                continue

            name = _first_name(employee)
            task_title = _spoken_task_title(getattr(task, "task_title", None))
            deadline_text = _deadline_text(task.deadline_date, task.deadline_time, timezone_name)

            # Overdue: once per day bucket while overdue.
            if now >= deadline_at:
                bucket = now.replace(hour=0, minute=0, second=0, microsecond=0)
                values = _event_notification_values(
                    employee=employee,
                    task=task,
                    notification_type=TYPE_TASK_OVERDUE,
                    title=f"Overdue: {task.task_title}",
                    message=f"Hey {name}, task #{task.id} \"{task_title}\" is overdue. Please review and update your progress.",
                    scheduled_at=bucket,
                    priority=PRIORITY_CRITICAL,
                    voice_text=(
                        f"Hey {name}, urgent alert. "
                        f"Your task {task_title} is overdue. "
                        f"Please review it now and update your progress."
                    ),
                    payload={
                        "notification_kind": "overdue",
                        "deadline": deadline_text,
                        "employee_name": getattr(employee, "name", None),
                        "task_title": task.task_title,
                    },
                    created_at=now,
                )
                created += self._insert_notification(session, values)
                continue

            remaining = deadline_at - now
            if remaining <= timedelta(minutes=15):
                bucket = _due_bucket(now, 15)
                priority = PRIORITY_CRITICAL
                label = _time_remaining_text(now, deadline_at) or "less than 15 minutes remaining"
                ntype = TYPE_DEADLINE_APPROACHING
            elif remaining <= timedelta(hours=1):
                bucket = _due_bucket(now, 30)
                priority = PRIORITY_HIGH
                label = _time_remaining_text(now, deadline_at) or "less than 1 hour remaining"
                ntype = TYPE_DEADLINE_APPROACHING
            elif remaining <= timedelta(hours=24):
                bucket = (deadline_at - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
                if now < bucket:
                    continue
                priority = PRIORITY_HIGH
                label = _time_remaining_text(now, deadline_at) or "about 24 hours remaining"
                ntype = TYPE_DEADLINE_APPROACHING
            else:
                continue

            values = _event_notification_values(
                employee=employee,
                task=task,
                notification_type=ntype,
                title=f"Deadline approaching: {task.task_title}",
                message=f"Hey {name}, task #{task.id} \"{task_title}\" is due soon. {label.capitalize()}.",
                scheduled_at=bucket,
                priority=priority,
                voice_text=(
                    f"Hey {name}, this is an important reminder for you. "
                    f"Your task {task_title} is due soon. "
                    f"You have {label}. Please complete it on time."
                ),
                payload={
                    "notification_kind": "deadline_approaching",
                    "deadline": deadline_text,
                    "time_remaining": label,
                    "employee_name": getattr(employee, "name", None),
                    "task_title": task.task_title,
                },
                created_at=now,
            )
            created += self._insert_notification(session, values)

        return created


    def _insert_notification(self, session, values: dict) -> int:
        dialect = session.bind.dialect.name if session.bind is not None else ""

        try:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert

                statement = (
                    insert(Notification)
                    .values(**values)
                    .on_conflict_do_nothing(
                        constraint="uq_notifications_employee_task_type_scheduled"
                    )
                )
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert

                statement = (
                    insert(Notification)
                    .values(**values)
                    .on_conflict_do_nothing()
                )
            else:
                session.add(Notification(**values))
                session.flush()
                return 1

            result = session.execute(statement)
            return max(int(result.rowcount or 0), 0)
        except IntegrityError:
            session.rollback()
            return 0

    def claim_pending_notifications(self, now: datetime) -> list[int]:
        session = self.session_factory()

        try:
            query = session.query(Notification).filter(
                Notification.delivery_status.in_(
                    (STATUS_PENDING, STATUS_RETRY)
                ),
                Notification.scheduled_at <= now,
                Notification.delivered_at.is_(None),
                or_(
                    Notification.next_retry_at.is_(None),
                    Notification.next_retry_at <= now,
                ),
            )

            dialect = session.bind.dialect.name if session.bind is not None else ""

            if dialect == "postgresql":
                query = query.with_for_update(skip_locked=True)

            rows = (
                query.order_by(Notification.scheduled_at, Notification.id)
                .limit(self.config.batch_size)
                .all()
            )

            ids = []

            for row in rows:
                row.delivery_status = STATUS_PROCESSING
                row.locked_at = now
                row.next_retry_at = None
                ids.append(int(row.id))

            session.commit()
            return ids
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _load_delivery_context(
        self, notification_id: int
    ) -> _DeliveryContext | None:
        session = self.session_factory()

        try:
            notification = session.query(Notification).filter(
                Notification.id == notification_id
            ).first()

            if notification is None:
                return None

            if notification.delivery_status != STATUS_PROCESSING:
                return None

            task = session.query(Task).filter(
                Task.id == notification.task_id
            ).first()
            employee = session.query(Employee).filter(
                Employee.id == notification.employee_id
            ).first()

            if task is None or employee is None:
                return None

            subscriptions = (
                session.query(PushSubscription)
                .filter(
                    PushSubscription.employee_id == notification.employee_id,
                    PushSubscription.is_active.is_(True),
                )
                .order_by(PushSubscription.id)
                .all()
            )

            payload = getattr(notification, "payload", None) or {}
            if not isinstance(payload, dict):
                payload = {}
            context = _DeliveryContext(
                notification_id=int(notification.id),
                employee_id=int(notification.employee_id),
                task_id=int(notification.task_id),
                task_title=task.task_title,
                task_status=(task.status or "PENDING"),
                deadline_date=task.deadline_date,
                deadline_time=task.deadline_time,
                timezone_name=employee_timezone(employee),
                title=notification.title,
                message=notification.message,
                notification_type=getattr(notification, "notification_type", NOTIFICATION_TYPE) or NOTIFICATION_TYPE,
                priority=normalize_priority(getattr(notification, "priority", None) or payload.get("priority")),
                voice_text=(getattr(notification, "voice_text", None) or payload.get("voice_text") or None),
                payload=payload,
                retry_count=int(notification.retry_count or 0),
                subscriptions=tuple(
                    {
                        "id": int(row.id),
                        "endpoint": row.endpoint,
                        "p256dh_key": row.p256dh_key,
                        "auth_key": row.auth_key,
                    }
                    for row in subscriptions
                ),
            )

            return context
        finally:
            session.close()

    def _push_payload(self, context: _DeliveryContext) -> dict:
        deadline = _deadline_text(
            context.deadline_date,
            context.deadline_time,
            context.timezone_name,
        )
        destination = (
            f"{self.config.frontend_url}/?task={context.task_id}"
            f"&notification={context.notification_id}"
        )

        return {
            "title": context.title,
            "body": context.message,
            "message": context.message,
            "deadline": deadline,
            "task_id": context.task_id,
            "notification_id": context.notification_id,
            "notification_type": getattr(context, "notification_type", NOTIFICATION_TYPE),
            "priority": getattr(context, "priority", PRIORITY_NORMAL),
            "voice_text": getattr(context, "voice_text", None),
            "url": destination,
            "destination_url": destination,
            "tag": f"task-notification-{context.notification_id}",
            "require_interaction": getattr(context, "priority", PRIORITY_NORMAL) in {PRIORITY_HIGH, PRIORITY_CRITICAL},
        }

    def deliver_notification(self, notification_id: int) -> str:
        now = self.now()
        context = self._load_delivery_context(notification_id)

        if context is None:
            return "cancelled"

        if (
            context.task_status.upper() not in OPEN_TASK_STATUSES
            or context.task_status.upper() == "CANCELLED"
        ):
            self._finish_notification(
                notification_id,
                STATUS_CANCELLED,
                now,
                "The task is no longer open.",
            )
            return "cancelled"

        session = self.session_factory()

        try:
            task = session.query(Task).filter(Task.id == context.task_id).first()
            employee = session.query(Employee).filter(
                Employee.id == context.employee_id
            ).first()

            still_owned = bool(
                task is not None
                and employee is not None
                and employee.is_active
                and int(task.assigned_to_employee_id or 0)
                == context.employee_id
            )
        finally:
            session.close()

        if not still_owned:
            self._finish_notification(
                notification_id,
                STATUS_CANCELLED,
                now,
                "The assignee changed or the employee is inactive.",
            )
            return "cancelled"

        logger.info(
            "push_delivery_context notification_id=%s employee_id=%s task_id=%s priority=%s retry_count=%s active_subscriptions=%s",
            notification_id,
            context.employee_id,
            context.task_id,
            context.priority,
            context.retry_count,
            len(context.subscriptions),
        )

        if not context.subscriptions:
            logger.info(
                "push_delivery_skipped notification_id=%s employee_id=%s reason=no_active_subscription",
                notification_id,
                context.employee_id,
            )
            self._finish_notification(
                notification_id,
                STATUS_NO_SUBSCRIPTION,
                now,
                "No active push subscription; the in-app notification is available.",
            )
            return "no_subscription"

        if not push_service.push_configured():
            logger.warning(
                "push_delivery_skipped notification_id=%s employee_id=%s reason=push_not_configured",
                notification_id,
                context.employee_id,
            )
            self._finish_notification(
                notification_id,
                STATUS_NO_SUBSCRIPTION,
                now,
                "Web Push is not configured; the in-app notification is available.",
            )
            return "no_subscription"

        payload = self._push_payload(context)
        successes = 0
        failures = 0
        permanent = 0
        terminal = 0
        errors = []

        for subscription in context.subscriptions:
            endpoint_origin = push_service.endpoint_origin(subscription["endpoint"])
            logger.info(
                "push_delivery_attempt notification_id=%s employee_id=%s subscription_id=%s provider_origin=%s retry_count=%s",
                notification_id,
                context.employee_id,
                subscription.get("id"),
                endpoint_origin,
                context.retry_count,
            )

            subscription_info = {
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh_key"],
                    "auth": subscription["auth_key"],
                },
            }

            try:
                result = self.push_sender(subscription_info, payload)
                successes += 1
                self._mark_subscription_used(
                    subscription["id"], now, active=True
                )
                status_code = getattr(result, "status_code", None)
                logger.info(
                    "push_delivery_accepted notification_id=%s employee_id=%s subscription_id=%s provider_origin=%s status=%s note=accepted_by_push_service_not_browser_display",
                    notification_id,
                    context.employee_id,
                    subscription.get("id"),
                    endpoint_origin,
                    status_code,
                )
            except push_service.PushDeliveryError as error:
                failures += 1
                errors.append(str(error))
                is_permanent = bool(
                    error.permanent or error.status_code in {404, 410}
                )
                is_terminal = not error.retryable and not is_permanent

                logger.warning(
                    "push_delivery_failure notification_id=%s employee_id=%s subscription_id=%s provider_origin=%s status=%s exception=%s permanent=%s auth_failure=%s retryable=%s retry_count=%s",
                    notification_id,
                    context.employee_id,
                    subscription.get("id"),
                    endpoint_origin,
                    getattr(error, "status_code", None),
                    type(error).__name__,
                    is_permanent,
                    bool(getattr(error, "auth_failure", False)),
                    bool(getattr(error, "retryable", True)),
                    context.retry_count,
                )

                if is_permanent:
                    permanent += 1
                    self._mark_subscription_used(
                        subscription["id"], now, active=False
                    )
                elif is_terminal:
                    terminal += 1
            except Exception as error:
                failures += 1
                errors.append(str(error))
                logger.exception(
                    "push_delivery_exception notification_id=%s employee_id=%s subscription_id=%s provider_origin=%s exception=%s retry_count=%s",
                    notification_id,
                    context.employee_id,
                    subscription.get("id"),
                    endpoint_origin,
                    type(error).__name__,
                    context.retry_count,
                )

        last_error = "; ".join(errors)[:4000] or None

        if successes > 0:
            status = STATUS_PARTIAL if failures else STATUS_SENT
            self._finish_notification(
                notification_id,
                status,
                now,
                last_error,
                delivered=True,
            )
            return "sent"

        if failures and permanent == failures:
            self._finish_notification(
                notification_id,
                STATUS_FAILED,
                now,
                last_error or "Every push subscription has expired.",
            )
            return "failed"

        # VAPID/config errors (401/403/400/missing library) cannot be fixed by
        # retrying the same request; surface them as failed so the cause is visible.
        if failures and (permanent + terminal) == failures:
            self._finish_notification(
                notification_id,
                STATUS_FAILED,
                now,
                last_error or "Web Push delivery failed due to configuration.",
            )
            return "failed"

        return self._retry_notification(
            notification_id,
            context.retry_count,
            now,
            last_error or "Push delivery failed.",
        )

    def _retry_notification(
        self,
        notification_id: int,
        retry_count: int,
        now: datetime,
        error: str,
    ) -> str:
        attempt = retry_count + 1

        if attempt > self.config.max_retries:
            self._finish_notification(
                notification_id,
                STATUS_FAILED,
                now,
                error,
            )
            return "failed"

        delay = min(
            self.config.retry_base_seconds * (2 ** max(attempt - 1, 0)),
            self.config.retry_max_seconds,
        )
        session = self.session_factory()

        try:
            row = session.query(Notification).filter(
                Notification.id == notification_id
            ).first()

            if row is None:
                return "failed"

            if row.delivery_status == STATUS_CANCELLED:
                return "cancelled"

            row.delivery_status = STATUS_RETRY
            row.retry_count = attempt
            row.next_retry_at = now + timedelta(seconds=delay)
            row.last_error = error[:4000]
            row.locked_at = None
            session.commit()

            return "retry"
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _finish_notification(
        self,
        notification_id: int,
        status: str,
        now: datetime,
        error: str | None,
        *,
        delivered: bool = False,
    ) -> None:
        session = self.session_factory()

        try:
            row = session.query(Notification).filter(
                Notification.id == notification_id
            ).first()

            if row is None:
                return

            if row.delivery_status == STATUS_CANCELLED and status != STATUS_CANCELLED:
                return

            row.delivery_status = status
            row.last_error = (error or "")[:4000] or None
            row.next_retry_at = None
            row.locked_at = None

            if delivered:
                row.delivered_at = now

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_subscription_used(
        self,
        subscription_id: int,
        now: datetime,
        *,
        active: bool,
    ) -> None:
        session = self.session_factory()

        try:
            row = session.query(PushSubscription).filter(
                PushSubscription.id == subscription_id
            ).first()

            if row is None:
                return

            row.last_used_at = now
            row.updated_at = now

            if not active:
                row.is_active = False

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _record_unexpected_failure(
        self, notification_id: int, now: datetime
    ) -> str:
        try:
            return self._retry_notification(
                notification_id,
                0,
                now,
                "Unexpected reminder delivery failure.",
            )
        except Exception:
            logger.exception(
                "Could not record reminder delivery failure for %s.",
                notification_id,
            )
            return "failed"
