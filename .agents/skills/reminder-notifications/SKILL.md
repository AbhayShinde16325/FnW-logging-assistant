# Reminder and Notification Skill

Use this skill when changing task reminders, notifications, Web Push, PWA
service-worker behaviour, reminder scheduling, or notification authorization.

## 1. Inspect before editing

Always read these first and confirm they still match the assumptions below:

- `database/models.py`, `database/connection.py`, `database/task_repository.py`
- `services/task_service.py`, `services/task_management.py`, `services/task_scope.py`
- `auth/service.py`, `auth/dependencies.py`, `auth/permissions.py`
- `app.py`, `reminder_worker.py`, `services/reminder_service.py`
- `services/notification_service.py`, `services/push_service.py`
- `frontend/src/App.jsx`, `frontend/src/auth.js`, `frontend/public/sw.js`
- `docker-compose.yml`, `Dockerfile`, `docker-entrypoint.sh`
- `migrations/versions/` and the newest migration head
- `tests/test_reminders_notifications.py` and existing auth/RBAC tests

Do not add a second reminder field, duplicate scheduler, client-trusted employee
id, or direct LLM notification insert.

## 2. Existing architecture

- FastAPI is the HTTP layer; `app.py` only parses requests and delegates.
- `services/task_service.py` owns conversational task behaviour.
- `services/task_management.py` owns authorized direct task CRUD.
- `database/task_repository.py` is the only module that talks to PostgreSQL
  for task data; writes use validated structured operations or ORM sessions.
- Employee identity comes from the access token and
  `auth.dependencies.get_current_employee`; never from request JSON, a name, or
  an LLM-produced id.
- `Task.assigned_to_employee_id` is the ownership source of truth.
  `Task.assigned_to` is display text only.
- Employees may read their assigned tasks and update their own progress.
  Managers and administrators have broader task access. Notification and
  push-subscription access is always self-only.

## 3. Reminder semantics

`Task.reminder_date` and `Task.reminder_time` already exist. Reuse them.

- Both fields are required for a reminder. If either is NULL, no reminder is
  scheduled; do not invent a default.
- Stored date/time values are the employee's local wall-clock values.
- `Employee.timezone` is an IANA timezone name, default `Asia/Kolkata`.
- At scheduling time convert local `date + time` through `ZoneInfo` to naive
  UTC for `notifications.scheduled_at`, which is stored in UTC.
- Validate: deadline date/time must be valid, reminder must be at or before the
  deadline, and a reminder in the past is not created.
- Relative reminders ("2 hours before the deadline") are resolved by
  `ai/reminder.py` and stored back into the existing reminder fields. Preserve
  conversational creation, clarification, confirmation, and update flows.
- Rescheduling, deadline changes, reassignment, completion, cancellation, and
  deletion must prevent an old pending reminder from firing. Cancel unsent
  notification rows for the task whenever those values change.

## 4. Database relationships

- `notifications.employee_id -> employees.id` (required, CASCADE on delete).
- `notifications.task_id -> tasks.id` (required, CASCADE on delete).
- `push_subscriptions.employee_id -> employees.id` (required, CASCADE).
- One employee may have many subscriptions; one endpoint is globally unique.
- Notification uniqueness: `(employee_id, task_id, notification_type,
  scheduled_at)`. This is the duplicate-prevention key. Do not replace it with
  an in-memory set.
- Keep notification rows when Web Push fails; the in-app center must still show
  them. Track `delivery_status`, `retry_count`, `next_retry_at`, `last_error`,
  `delivered_at`, and `locked_at`.
- Use additive Alembic migrations. Never drop tables, delete task rows, or
  recreate PostgreSQL volumes. Use `inspector` guards so deployments that
  already have some objects are safe.

## 5. Worker and scheduling rules

- Run a dedicated `reminder-worker` Docker service, not a scheduler inside each
  FastAPI process. It polls roughly every 15–30 seconds.
- The worker finds open tasks with both reminder fields whose reminder instant
  is due in UTC, verifies the task still exists, is not COMPLETED/CANCELLED,
  and that the assigned employee still exists, is active, and still owns it.
- Create the notification first with the unique key and
  `ON CONFLICT DO NOTHING` (or the SQLAlchemy equivalent). Then claim the row
  with `FOR UPDATE SKIP LOCKED` when PostgreSQL supports it, commit, and release
  the transaction before network delivery.
- Multiple workers must not create or send the same notification twice.
- Recover rows left in `PROCESSING` after a crash using a stale-lock timeout.
- Retry transient push failures with bounded exponential backoff and a maximum
  retry count. Permanent failures (404/410, expired endpoint) deactivate or
  delete the subscription. Never crash the worker on one bad subscription.
- Do not send reminders older than the configured max-age window.
- Use an injectable clock in tests so a full flow runs in seconds.

## 6. Push lifecycle

- Web Push uses VAPID. The private key is server-only and must never appear in
  frontend code, API responses, logs, or Git.
- Config names: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` or
  `VAPID_PRIVATE_KEY_PATH`, `VAPID_SUBJECT`.
- `POST /push/subscribe` validates endpoint and keys, derives the employee from
  auth, and updates the device row. An endpoint owned by another employee is a
  409; never reassign it.
- `DELETE /push/subscribe` removes only the caller's subscription.
- Expired endpoints are cleaned up after 404/410.
- Payload: title, message, deadline, task id, notification id, destination URL.
- The service worker shows the notification and, on click, focuses/opens the
  task URL.
- HTTPS is required outside localhost. iOS requires Home Screen installation
  and user permission before Web Push works.
- Sound: an optional short sound is allowed only while the app is open and the
  browser permits it. Never claim a site can force sound, bypass silent mode,
  or override Do Not Disturb; never autoplay against browser policy.

## 7. Authorization and API

- Notification list/count/read operations are always filtered by the
  authenticated employee id. No `employee_id` query/body parameter is trusted.
- Managers and administrators do not automatically see another employee's
  personal notifications or device credentials unless a future business
  requirement explicitly adds that visibility.
- Reuse `require_permission` and 401/403 conventions.
- Suggested self-scoped endpoints:
  - `GET /notifications`
  - `GET /notifications/unread-count`
  - `PATCH /notifications/{id}/read`
  - `POST /notifications/read-all`
  - `GET /push/public-key`
  - `POST /push/subscribe`
  - `DELETE /push/subscribe`
  - `GET/PATCH /notifications/settings` for timezone
- Validate timezone names with `ZoneInfo`; reject unknown names.
- The LLM may update task reminder fields through authorized task services but
  must never insert notifications or send push directly.

## 8. Testing and verification

Use a temporary SQLite or PostgreSQL database and monkeypatch the clock and push
provider. Cover:

- notification creation and FK relationships;
- uniqueness/duplicate prevention;
- due/future/completed/deleted/rescheduled/reassigned/restarted cases;
- two workers claiming one row;
- employee isolation, foreign subscriptions, deactivated employees;
- payload construction, 404/410 cleanup, retries, click URL;
- frontend permission denied, unread count, read actions, task navigation;
- an end-to-end test with reminder two minutes ahead using an injected clock.

Run at minimum:

```bash
AGENTIC_READ=0 DATABASE_AGENT=0 python -m pytest tests -q
cd frontend && npm run lint && npm test && npm run build
```

Also validate the Alembic head on a temporary database and start the Docker
Compose stack to confirm `reminder-worker` is running. Do not claim device push
delivery unless a real browser subscription was tested.

## 9. Safe migration checklist

1. Read the current migration head and `DATABASE_SCHEMA.md`.
2. Add a new revision; do not edit applied migrations.
3. Add tables/columns/indexes only when missing.
4. Use server defaults for new non-null columns.
5. Keep foreign-key deletion behaviour explicit.
6. Preserve existing data and reminder fields.
7. Test `alembic upgrade head` on a fresh temporary DB.
8. Document new environment variables and HTTPS/PWA requirements.
