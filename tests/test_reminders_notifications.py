"""Reminder scheduling, notification authorization and Web Push tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import joinedload, sessionmaker
from sqlalchemy.pool import StaticPool

import app as app_module
import auth.config as auth_config
import auth.service as auth_service
import database.task_repository as task_repository
import services.notification_service as notification_service
import services.push_service as push_service
import services.task_management as task_management
from auth.permissions import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_MANAGER
from auth.security import create_access_token
from auth.service import bootstrap_admin, create_session, sync_rbac
from database.connection import Base
from database.models import Employee, Notification, PushSubscription, Role, Task
from services.notification_service import (
    STATUS_CANCELLED,
    STATUS_NO_SUBSCRIPTION,
    STATUS_PARTIAL,
    STATUS_PROCESSING,
    STATUS_RETRY,
    STATUS_SENT,
    ReminderWorker,
    ReminderWorkerConfig,
)

UTC = timezone.utc
TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 17, 12, 0)
FRONTEND = "https://tasks.example.com"


@pytest.fixture
def reminder_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for module in (
        auth_service,
        notification_service,
        task_management,
        task_repository,
    ):
        monkeypatch.setattr(module, "SessionLocal", factory)

    monkeypatch.setattr(app_module, "create_tables", lambda: None)
    settings = replace(
        auth_config.load_auth_settings(),
        required=True,
        secret_key="reminder-test-secret-key-that-is-long-enough-123",
        google_client_id="google-client",
        google_client_secret="google-secret",
        access_token_minutes=30,
        refresh_token_days=14,
    )
    monkeypatch.setattr(auth_config, "settings", settings)
    sync_rbac()

    admin = bootstrap_admin(email="admin@example.com", name="Admin")
    employee_id = _add_employee(factory, "employee@example.com", "Rahul")
    other_id = _add_employee(factory, "other@example.com", "Neha")

    with TestClient(app_module.app) as client:
        yield SimpleNamespace(
            client=client,
            factory=factory,
            settings=settings,
            admin=admin,
            employee_id=employee_id,
            other_id=other_id,
            employee_headers=_headers(factory, settings, employee_id)[0],
            other_headers=_headers(factory, settings, other_id)[0],
        )

    Base.metadata.drop_all(engine)
    engine.dispose()


def _add_employee(factory, email, name, *, timezone_name="Asia/Kolkata", active=True):
    session = factory()

    try:
        role = session.query(Role).filter(Role.name == ROLE_EMPLOYEE).one()
        row = Employee(
            name=name,
            email=email,
            role_id=role.id,
            is_active=active,
            timezone=timezone_name,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)
    finally:
        session.close()


def _headers(factory, settings, employee_id):
    session = factory()

    try:
        employee = (
            session.query(Employee)
            .options(joinedload(Employee.role))
            .filter(Employee.id == employee_id)
            .one()
        )
        permissions = [item.name for item in employee.role.permissions]
        email = employee.email
        role = employee.role.name
    finally:
        session.close()

    refresh_token, row = create_session(employee_id, auth_settings=settings)
    token, _ = create_access_token(
        employee_id=employee_id,
        email=email,
        role=role,
        permissions=permissions,
        session_id=row.id,
        settings=settings,
    )

    return {"Authorization": f"Bearer {token}"}, row.id


def _local_parts(value: datetime, timezone_name="Asia/Kolkata"):
    local = value.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone_name))
    return local.date(), local.time().replace(tzinfo=None, microsecond=0)


def _add_task(
    factory,
    *,
    employee_id,
    title="Prepare report",
    reminder_at=None,
    deadline_at=None,
    status="PENDING",
    timezone_name="Asia/Kolkata",
):
    reminder_at = reminder_at or (NOW - timedelta(minutes=1))
    deadline_at = deadline_at or (NOW + timedelta(hours=2))
    reminder_date, reminder_time = _local_parts(reminder_at, timezone_name)
    deadline_date, deadline_time = _local_parts(deadline_at, timezone_name)
    session = factory()

    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).one()
        task = Task(
            task_title=title,
            assigned_to=employee.name,
            assigned_to_employee_id=employee_id,
            deadline_date=deadline_date,
            deadline_time=deadline_time,
            reminder_date=reminder_date,
            reminder_time=reminder_time,
            status=status,
            progress=100 if status == "COMPLETED" else 0,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return int(task.id)
    finally:
        session.close()


def _add_subscription(factory, employee_id, endpoint="https://push.example.com/one"):
    session = factory()

    try:
        row = PushSubscription(
            employee_id=employee_id,
            endpoint=endpoint,
            p256dh_key="p256dh-key",
            auth_key="auth-key",
            device_name="Test browser",
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)
    finally:
        session.close()


def _worker(factory, *, clock=lambda: NOW, sender=None, **overrides):
    config = ReminderWorkerConfig(
        poll_seconds=1,
        batch_size=20,
        max_retries=3,
        retry_base_seconds=30,
        retry_max_seconds=300,
        max_age_hours=168,
        processing_timeout_seconds=300,
        frontend_url=FRONTEND,
        proactive_alerts=False,
    )
    config = replace(config, **overrides)

    return ReminderWorker(
        config=config,
        clock=clock,
        session_factory=factory,
        push_sender=sender or (lambda subscription, payload: None),
    )


def _notifications(factory):
    session = factory()

    try:
        return session.query(Notification).order_by(Notification.id).all()
    finally:
        session.close()


# --- API and authorization -------------------------------------------------


def test_employee_sees_only_their_notifications_and_can_mark_read(reminder_env):
    session = reminder_env.factory()

    try:
        own = Notification(
            employee_id=reminder_env.employee_id,
            task_id=_add_task(
                reminder_env.factory,
                employee_id=reminder_env.employee_id,
            ),
            title="Own",
            message="Own reminder",
            notification_type="TASK_REMINDER",
            is_read=False,
            scheduled_at=NOW,
            created_at=NOW,
            delivery_status=STATUS_SENT,
        )
        other_task = _add_task(
            reminder_env.factory,
            employee_id=reminder_env.other_id,
            title="Other",
        )
        other = Notification(
            employee_id=reminder_env.other_id,
            task_id=other_task,
            title="Other",
            message="Other reminder",
            notification_type="TASK_REMINDER",
            is_read=False,
            scheduled_at=NOW,
            created_at=NOW,
            delivery_status=STATUS_SENT,
        )
        session.add_all([own, other])
        session.commit()
        own_id = int(own.id)
        other_id = int(other.id)
    finally:
        session.close()

    listing = reminder_env.client.get(
        "/notifications", headers=reminder_env.employee_headers
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["unread_count"] == 1
    assert [item["title"] for item in body["notifications"]] == ["Own"]
    assert (
        f"task={body['notifications'][0]['task_id']}"
        in body["notifications"][0]["destination_url"]
    )

    other_read = reminder_env.client.patch(
        f"/notifications/{other_id}/read",
        headers=reminder_env.employee_headers,
    )
    assert other_read.status_code == 404

    read = reminder_env.client.patch(
        f"/notifications/{own_id}/read",
        headers=reminder_env.employee_headers,
    )
    assert read.status_code == 200
    assert read.json()["unread_count"] == 0

    count = reminder_env.client.get(
        "/notifications/unread-count",
        headers=reminder_env.employee_headers,
    )
    assert count.json() == {"unread_count": 0}


def test_mark_all_and_settings_timezone(reminder_env):
    task_id = _add_task(
        reminder_env.factory, employee_id=reminder_env.employee_id
    )
    session = reminder_env.factory()

    try:
        session.add_all(
            [
                Notification(
                    employee_id=reminder_env.employee_id,
                    task_id=task_id,
                    title=f"Reminder {index}",
                    message="Message",
                    notification_type="TASK_REMINDER",
                    is_read=False,
                    scheduled_at=NOW + timedelta(minutes=index),
                    created_at=NOW,
                    delivery_status=STATUS_SENT,
                )
                for index in range(2)
            ]
        )
        session.commit()
    finally:
        session.close()

    marked = reminder_env.client.post(
        "/notifications/read-all",
        headers=reminder_env.employee_headers,
    )
    assert marked.status_code == 200
    assert marked.json()["updated"] == 2

    settings = reminder_env.client.get(
        "/notifications/settings",
        headers=reminder_env.employee_headers,
    )
    assert settings.json()["timezone"] == "Asia/Kolkata"

    updated = reminder_env.client.patch(
        "/notifications/settings",
        headers=reminder_env.employee_headers,
        json={"timezone": "Europe/London"},
    )
    assert updated.status_code == 200
    assert updated.json()["timezone"] == "Europe/London"

    invalid = reminder_env.client.patch(
        "/notifications/settings",
        headers=reminder_env.employee_headers,
        json={"timezone": "Mars/Olympus_Mons"},
    )
    assert invalid.status_code == 400


def test_push_subscription_is_self_scoped_and_keys_are_not_returned(reminder_env):
    endpoint = "https://push.example.com/browser-1"
    created = reminder_env.client.post(
        "/push/subscribe",
        headers=reminder_env.employee_headers,
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": "browser-key", "auth": "browser-auth"},
            "device_name": "Office Chrome",
        },
    )
    assert created.status_code == 201
    assert "browser-key" not in created.text

    duplicate = reminder_env.client.post(
        "/push/subscribe",
        headers=reminder_env.other_headers,
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": "other-key", "auth": "other-auth"},
        },
    )
    assert duplicate.status_code == 409

    own_list = reminder_env.client.get(
        "/push/subscriptions",
        headers=reminder_env.employee_headers,
    )
    assert own_list.status_code == 200
    assert own_list.json()["count"] == 1
    assert "endpoint" not in own_list.text

    other_list = reminder_env.client.get(
        "/push/subscriptions",
        headers=reminder_env.other_headers,
    )
    assert other_list.json()["count"] == 0

    wrong_delete = reminder_env.client.request(
        "DELETE",
        "/push/subscribe",
        headers=reminder_env.other_headers,
        json={"endpoint": endpoint},
    )
    assert wrong_delete.status_code == 404

    deleted = reminder_env.client.request(
        "DELETE",
        "/push/subscribe",
        headers=reminder_env.employee_headers,
        json={"endpoint": endpoint},
    )
    assert deleted.status_code == 200


# --- scheduler -------------------------------------------------------------


def test_due_reminder_creates_exactly_one_notification(reminder_env):
    _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    worker = _worker(reminder_env.factory, sender=lambda subscription, payload: None)

    first = worker.run_once()
    second = worker.run_once()

    rows = _notifications(reminder_env.factory)
    assert first["created"] == 1
    assert second["created"] == 0
    assert len(rows) == 1
    assert rows[0].delivery_status == STATUS_NO_SUBSCRIPTION
    assert rows[0].employee_id == reminder_env.employee_id


def test_future_reminder_does_not_fire_early_and_completed_is_skipped(reminder_env):
    _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        title="Future",
        reminder_at=NOW + timedelta(minutes=5),
    )
    _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        title="Done",
        status="COMPLETED",
    )

    worker = _worker(reminder_env.factory)
    result = worker.run_once()

    assert result["created"] == 0
    assert _notifications(reminder_env.factory) == []


def test_reminder_becomes_due_with_injected_clock(reminder_env):
    _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        reminder_at=NOW + timedelta(minutes=2),
    )
    current = [NOW]

    worker = _worker(reminder_env.factory, clock=lambda: current[0])
    assert worker.run_once()["created"] == 0

    current[0] = NOW + timedelta(minutes=2)
    assert worker.run_once()["created"] == 1

    current[0] = NOW + timedelta(minutes=3)
    assert worker.run_once()["created"] == 0
    assert len(_notifications(reminder_env.factory)) == 1


def test_reassigned_task_notifies_current_employee(reminder_env):
    task_id = _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        reminder_at=NOW + timedelta(minutes=2),
    )
    session = reminder_env.factory()

    try:
        task = session.query(Task).filter(Task.id == task_id).one()
        employee = session.query(Employee).filter(
            Employee.id == reminder_env.other_id
        ).one()
        task.assigned_to_employee_id = employee.id
        task.assigned_to = employee.name
        session.commit()
    finally:
        session.close()

    worker = _worker(reminder_env.factory, clock=lambda: NOW + timedelta(minutes=2))
    worker.run_once()

    rows = _notifications(reminder_env.factory)
    assert len(rows) == 1
    assert rows[0].employee_id == reminder_env.other_id


def test_deactivated_employee_and_deleted_task_are_skipped(reminder_env):
    inactive_id = _add_employee(
        reminder_env.factory,
        "inactive@example.com",
        "Inactive",
        active=False,
    )
    _add_task(reminder_env.factory, employee_id=inactive_id, title="Inactive")

    task_id = _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        title="Delete me",
    )
    session = reminder_env.factory()

    try:
        session.delete(session.query(Task).filter(Task.id == task_id).one())
        session.commit()
    finally:
        session.close()

    result = _worker(reminder_env.factory).run_once()

    assert result["created"] == 0
    assert _notifications(reminder_env.factory) == []


def test_rescheduling_cancels_old_pending_notification(reminder_env):
    task_id = _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    _add_subscription(reminder_env.factory, reminder_env.employee_id)

    def transient(_subscription, _payload):
        raise push_service.PushDeliveryError("temporary failure", status_code=503)

    worker = _worker(reminder_env.factory, sender=transient)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        worker.run_once()

    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status == STATUS_RETRY

    task_repository.update_task(
        task_id,
        {
            "reminder_date": (NOW + timedelta(days=2)).date(),
            "reminder_time": time(9, 0),
        },
    )

    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status == STATUS_CANCELLED

    delivered = []

    def success(subscription, payload):
        delivered.append(payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        _worker(
            reminder_env.factory,
            clock=lambda: NOW + timedelta(days=1),
            sender=success,
        ).run_once()

    assert delivered == []


def test_restart_recovery_reclaims_stale_processing(reminder_env):
    _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    session = reminder_env.factory()

    try:
        task = session.query(Task).first()
        row = Notification(
            employee_id=reminder_env.employee_id,
            task_id=task.id,
            title="Stale",
            message="Stale reminder",
            notification_type="TASK_REMINDER",
            is_read=False,
            scheduled_at=NOW - timedelta(minutes=10),
            created_at=NOW - timedelta(minutes=10),
            delivery_status=STATUS_PROCESSING,
            locked_at=NOW - timedelta(minutes=10),
            retry_count=0,
        )
        session.add(row)
        session.commit()
    finally:
        session.close()

    result = _worker(reminder_env.factory).run_once()

    assert result["recovered"] == 1
    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status == STATUS_NO_SUBSCRIPTION


def test_second_worker_does_not_claim_the_same_row(reminder_env):
    _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    worker = _worker(reminder_env.factory)

    worker.create_due_notifications(NOW)
    first_claim = worker.claim_pending_notifications(NOW)
    second_claim = worker.claim_pending_notifications(NOW)

    assert len(first_claim) == 1
    assert second_claim == []


# --- push delivery ---------------------------------------------------------


def test_transient_failure_retries_then_sends_once(reminder_env):
    _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    _add_subscription(reminder_env.factory, reminder_env.employee_id)
    calls = []
    mode = {"fail": True}

    def sender(subscription, payload):
        calls.append(payload)
        if mode["fail"]:
            raise push_service.PushDeliveryError("temporary", status_code=503)

    current = [NOW]
    worker = _worker(reminder_env.factory, clock=lambda: current[0], sender=sender)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        worker.run_once()

    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status == STATUS_RETRY
    assert row.retry_count == 1

    mode["fail"] = False
    current[0] = NOW + timedelta(seconds=31)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        worker.run_once()
        worker.run_once()

    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status == STATUS_SENT
    assert len(calls) == 2
    assert calls[0]["task_id"] == row.task_id
    assert calls[0]["notification_id"] == row.id
    assert calls[0]["url"].startswith(FRONTEND)


def test_expired_subscription_is_deactivated(reminder_env):
    _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    subscription_id = _add_subscription(
        reminder_env.factory, reminder_env.employee_id
    )

    def expired(_subscription, _payload):
        raise push_service.PushDeliveryError(
            "expired", status_code=410, permanent=True
        )

    worker = _worker(reminder_env.factory, sender=expired)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        worker.run_once()

    session = reminder_env.factory()

    try:
        subscription = session.query(PushSubscription).filter(
            PushSubscription.id == subscription_id
        ).one()
        assert subscription.is_active is False
    finally:
        session.close()

    row = _notifications(reminder_env.factory)[0]
    assert row.delivery_status not in {STATUS_SENT, STATUS_PARTIAL}
    assert "expired" in (row.last_error or "").lower()


def test_end_to_end_reminder_flow_and_click_destination(reminder_env):
    task_id = _add_task(
        reminder_env.factory,
        employee_id=reminder_env.employee_id,
        title="E2E task",
        reminder_at=NOW + timedelta(minutes=2),
        deadline_at=NOW + timedelta(minutes=10),
    )
    _add_subscription(reminder_env.factory, reminder_env.employee_id)
    payloads = []
    current = [NOW]

    def sender(_subscription, payload):
        payloads.append(payload)

    worker = _worker(reminder_env.factory, clock=lambda: current[0], sender=sender)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        assert worker.run_once()["created"] == 0
        assert reminder_env.client.get(
            "/notifications/unread-count",
            headers=reminder_env.employee_headers,
        ).json()["unread_count"] == 0

        current[0] = NOW + timedelta(minutes=2)
        result = worker.run_once()

    assert result["created"] == 1
    assert len(payloads) == 1
    assert payloads[0]["task_id"] == task_id
    assert payloads[0]["deadline"]
    assert f"task={task_id}" in payloads[0]["url"]

    count = reminder_env.client.get(
        "/notifications/unread-count",
        headers=reminder_env.employee_headers,
    )
    assert count.json()["unread_count"] == 1

    listing = reminder_env.client.get(
        "/notifications", headers=reminder_env.employee_headers
    )
    notification = listing.json()["notifications"][0]
    assert notification["task_id"] == task_id
    assert notification["delivery_status"] == STATUS_SENT

    read = reminder_env.client.patch(
        f"/notifications/{notification['id']}/read",
        headers=reminder_env.employee_headers,
    )
    assert read.json()["unread_count"] == 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(push_service, "push_configured", lambda: True)
        worker.run_once()
        worker.run_once()

    assert len(payloads) == 1
    assert len(_notifications(reminder_env.factory)) == 1


def test_task_api_rejects_incomplete_or_after_deadline_reminder(reminder_env):
    session = reminder_env.factory()

    try:
        role = session.query(Role).filter(Role.name == ROLE_MANAGER).one()
        manager = Employee(
            name="Manager",
            email="manager@example.com",
            role_id=role.id,
            is_active=True,
            timezone="Asia/Kolkata",
        )
        session.add(manager)
        session.commit()
        session.refresh(manager)
        manager_id = int(manager.id)
    finally:
        session.close()

    manager_headers = _headers(
        reminder_env.factory, reminder_env.settings, manager_id
    )[0]
    assigned = reminder_env.employee_id

    incomplete = reminder_env.client.post(
        "/tasks",
        headers=manager_headers,
        json={
            "task_title": "Incomplete reminder",
            "assigned_to_employee_id": assigned,
            "reminder_date": "2026-11-18",
        },
    )
    assert incomplete.status_code == 400

    after_deadline = reminder_env.client.post(
        "/tasks",
        headers=manager_headers,
        json={
            "task_title": "Late reminder",
            "assigned_to_employee_id": assigned,
            "deadline_date": "2026-11-18",
            "deadline_time": "10:00:00",
            "reminder_date": "2026-11-18",
            "reminder_time": "11:00:00",
        },
    )
    assert after_deadline.status_code == 400


def test_agent_style_update_returns_success_and_reschedules_reminders(reminder_env):
    task_id = _add_task(reminder_env.factory, employee_id=reminder_env.employee_id)
    session = reminder_env.factory()

    try:
        session.add(
            Notification(
                employee_id=reminder_env.employee_id,
                task_id=task_id,
                title="Old reminder",
                message="Old reminder body",
                notification_type="TASK_REMINDER",
                is_read=False,
                scheduled_at=NOW,
                created_at=NOW,
                delivery_status="PENDING",
            )
        )
        session.commit()
    finally:
        session.close()

    result = task_repository.execute_write_sql(
        (
            "UPDATE tasks SET reminder_date = :reminder_date, "
            "reminder_time = :reminder_time WHERE id = :task_id"
        ),
        {
            "reminder_date": (NOW + timedelta(days=1)).date().isoformat(),
            "reminder_time": "09:00:00",
            "task_id": task_id,
        },
        confirmation="CONFIRM UPDATE",
        operation="UPDATE",
    )

    assert result["status"] == "success"

    assert _notifications(reminder_env.factory)[0].delivery_status == STATUS_CANCELLED

    listed = reminder_env.client.get(
        "/notifications",
        headers=reminder_env.employee_headers,
    ).json()
    assert listed["notifications"] == []
    assert listed["unread_count"] == 0
