"""Self-scoped notification, timezone and Web Push HTTP endpoints."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from auth.dependencies import require_permission
from auth.permissions import TASKS_READ
from auth.service import AuthenticatedEmployee
from services import notification_service
from services import push_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


class NotificationReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimezoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=64)


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


class PushSubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=1024)
    keys: PushKeys
    device_name: str | None = Field(default=None, max_length=255)


class PushUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=1024)


def _actor(employee: AuthenticatedEmployee | None) -> AuthenticatedEmployee:
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required for personal notifications.",
        )

    return employee


@router.get("/notifications")
def list_notifications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = False,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)
    rows, total = notification_service.list_notifications(
        actor.id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )

    return {
        "count": len(rows),
        "total": total,
        "unread_count": notification_service.unread_notification_count(actor.id),
        "notifications": [
            notification_service.serialize_notification(row) for row in rows
        ],
    }


@router.get("/notifications/unread-count")
def unread_count(
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)

    return {
        "unread_count": notification_service.unread_notification_count(actor.id)
    }


@router.get("/notifications/settings")
def get_notification_settings(
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)
    timezone = notification_service.get_employee_timezone(actor.id)

    return {
        "timezone": timezone or actor.timezone,
        "push_configured": push_service.push_configured(),
    }


@router.patch("/notifications/settings")
def update_notification_settings(
    request: TimezoneRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)

    try:
        timezone = notification_service.set_employee_timezone(
            actor.id, request.timezone
        )
    except notification_service.ReminderValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        )

    return {
        "timezone": timezone,
        "push_configured": push_service.push_configured(),
    }


@router.patch("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)

    if not notification_service.mark_notification_read(
        actor.id, notification_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The notification was not found.",
        )

    return {
        "status": "success",
        "notification_id": notification_id,
        "unread_count": notification_service.unread_notification_count(actor.id),
    }


@router.post("/notifications/read-all")
def mark_all_notifications_read(
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)
    updated = notification_service.mark_all_notifications_read(actor.id)

    return {
        "status": "success",
        "updated": updated,
        "unread_count": 0,
    }


@router.get("/push/public-key")
def public_vapid_key():
    key = push_service.vapid_public_key()

    return {
        "public_key": key or None,
        "configured": push_service.push_configured(),
    }


@router.get("/push/subscriptions")
def list_push_subscriptions(
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)
    rows = notification_service.list_push_subscriptions(actor.id)

    return {
        "count": len(rows),
        "subscriptions": [
            {
                "id": int(row.id),
                "device_name": row.device_name,
                "created_at": (
                    row.created_at.isoformat() + "Z"
                    if row.created_at
                    else None
                ),
                "updated_at": (
                    row.updated_at.isoformat() + "Z"
                    if row.updated_at
                    else None
                ),
                "last_used_at": (
                    row.last_used_at.isoformat() + "Z"
                    if row.last_used_at
                    else None
                ),
            }
            for row in rows
        ],
    }


def _push_test_enabled() -> bool:
    return os.getenv("PUSH_TEST_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@router.post("/push/test")
def send_test_push(
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    """Development-only: send an immediate test push to the current employee.

    Disabled by default. Enable with PUSH_TEST_ENABLED=1. Uses existing push
    service and active subscriptions only; does not create task records.
    """

    if not _push_test_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Push test endpoint is not enabled.",
        )

    actor = _actor(employee)
    subscriptions = notification_service.list_push_subscriptions(actor.id)

    logger.info(
        "push_test_requested employee_id=%s active_subscriptions=%s",
        actor.id,
        len(subscriptions),
    )

    if not subscriptions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active push subscription found for this employee.",
        )

    if not push_service.push_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Web Push is not configured on the server.",
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    payload = {
        "title": "Task Flow push test",
        "body": "If you see this while the tab is closed, background Web Push is working.",
        "message": "If you see this while the tab is closed, background Web Push is working.",
        "task_id": None,
        "notification_id": None,
        "notification_type": "PUSH_TEST",
        "priority": "HIGH",
        "url": os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/") + "/",
        "destination_url": "/",
        "tag": f"push-test-{actor.id}",
        "require_interaction": True,
        "timestamp": now,
    }

    attempts = []
    any_accepted = False

    for row in subscriptions:
        origin = push_service.endpoint_origin(row.endpoint)
        info = {
            "endpoint": row.endpoint,
            "keys": {"p256dh": row.p256dh_key, "auth": row.auth_key},
        }

        try:
            result = push_service.send_web_push(info, payload)
            any_accepted = True
            attempts.append(
                {
                    "subscription_id": int(row.id),
                    "provider_origin": origin,
                    "accepted": True,
                    "status": getattr(result, "status_code", None),
                }
            )
            logger.info(
                "push_test_accepted employee_id=%s subscription_id=%s provider_origin=%s status=%s",
                actor.id,
                row.id,
                origin,
                getattr(result, "status_code", None),
            )
        except push_service.PushDeliveryError as error:
            attempts.append(
                {
                    "subscription_id": int(row.id),
                    "provider_origin": origin,
                    "accepted": False,
                    "status": getattr(error, "status_code", None),
                    "exception": type(error).__name__,
                    "permanent": bool(getattr(error, "permanent", False)),
                    "retryable": bool(getattr(error, "retryable", False)),
                }
            )
            logger.warning(
                "push_test_failure employee_id=%s subscription_id=%s provider_origin=%s status=%s exception=%s",
                actor.id,
                row.id,
                origin,
                getattr(error, "status_code", None),
                type(error).__name__,
            )
        except Exception as error:
            attempts.append(
                {
                    "subscription_id": int(row.id),
                    "provider_origin": origin,
                    "accepted": False,
                    "exception": type(error).__name__,
                }
            )
            logger.exception(
                "push_test_exception employee_id=%s subscription_id=%s provider_origin=%s",
                actor.id,
                row.id,
                origin,
            )

    return {
        "status": "accepted" if any_accepted else "failed",
        "accepted_by_push_service": any_accepted,
        "note": "Acceptance means the push service took the message. It does not prove the browser received the event or displayed a notification.",
        "employee_id": actor.id,
        "attempts": attempts,
    }


@router.post("/push/subscribe", status_code=status.HTTP_201_CREATED)
def subscribe_to_push(
    request: PushSubscribeRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)

    try:
        row = notification_service.register_push_subscription(
            actor.id,
            endpoint=request.endpoint,
            p256dh=request.keys.p256dh,
            auth=request.keys.auth,
            device_name=request.device_name,
        )
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        )
    except notification_service.ReminderValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        )

    return {
        "status": "success",
        "subscription": {
            "id": int(row.id),
            "device_name": row.device_name,
            "is_active": bool(row.is_active),
        },
    }


@router.delete("/push/subscribe")
def unsubscribe_from_push(
    request: PushUnsubscribeRequest,
    employee: AuthenticatedEmployee | None = Depends(
        require_permission(TASKS_READ)
    ),
):
    actor = _actor(employee)

    if not notification_service.unregister_push_subscription(
        actor.id, request.endpoint
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The push subscription was not found.",
        )

    return {"status": "success"}
