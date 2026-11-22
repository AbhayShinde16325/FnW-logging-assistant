# Database Schema Reference (SQL)

Reference for generating SQL against the Task Flow Automation database.
Source of truth: `database/models.py`, `database/connection.py`,
`database/task_repository.py`. If the ORM model changes, update this file.

## Guardrails (MUST follow)

These guardrails are non-negotiable and apply to every generated or executed
statement.

- Only the `tasks` table may be referenced. No other tables, no joins, no
  schema changes (`ALTER`, `CREATE TABLE`, etc.).
  The application-internal table `conversation_memory` (see below) is not part
  of this skill: it is only read and written through the ORM.
- Allowed operations are `SELECT`, `INSERT`, `UPDATE`, `DELETE`, and
  `DROP TABLE tasks`.
- `SELECT` and `INSERT` require no confirmation.
- `UPDATE`, `DELETE`, and `DROP TABLE tasks` require explicit user confirmation
  before execution.
- Before confirmation, show the exact SQL, parameters, target rows/table, and
  explain the impact.
- `DELETE` must include a specific `WHERE` clause; never allow an unrestricted
  `DELETE`.
- `DROP TABLE tasks` must identify the exact table and clearly warn that the
  entire table and all rows will be removed.
- Never treat general conversation or ambiguous responses as confirmation.
- `TRUNCATE`, `ALTER`, `CREATE DATABASE`, and unrelated operations are
  prohibited.
- Use parameterized values; never inline untrusted input.

## Connection

- Engine: PostgreSQL 16 (Docker service `db`, container `fnw-postgres`).
- URL: `DATABASE_URL` from `.env`, e.g.
  `postgresql+psycopg://postgres:<password>@localhost:5432/fnw_task_logger`
- Schema: default (`public`). No custom schema.
- Schema changes are owned by Alembic (`alembic upgrade head`). The backend
  container runs it before startup. `create_tables()` remains as a safe
  bootstrap for local runs.
- The task table is the only table the SQL generation skill may reference.
  Auth, RBAC, session, conversation-memory, notification, and push-subscription
  tables are application-internal and are accessed through the ORM only.
- Foreign keys exist on the auth/RBAC/task-employee columns, but SQL generated
  against `tasks` still never uses joins.

## Table: `tasks`

Mapped class: `database.models.Task`.

| Column          | SQLAlchemy type | Postgres type        | Nullable | Default     | Constraints / notes                       |
| --------------- | --------------- | -------------------- | -------- | ----------- | ----------------------------------------- |
| `id`            | `Integer`       | `integer`            | NOT NULL | autoincrement | Primary key, indexed (`ix_tasks_id`).     |
| `task_title`    | `String`        | `varchar`            | NOT NULL | none        | Required. Use non-empty text.             |
| `task_description` | `String`     | `varchar`            | NULL     | none        | Description or expected outcome. Required by the progressive-create flow. |
| `assigned_to`   | `String`        | `varchar`            | NULL     | none        | Legacy display name or `NULL`.            |
| `created_by_employee_id` | `Integer` | `integer`     | NULL     | none        | FK to `employees.id`, `ON DELETE SET NULL`. |
| `assigned_to_employee_id` | `Integer` | `integer`   | NULL     | none        | FK to `employees.id`, `ON DELETE SET NULL`. |
| `assigned_date` | `Date`          | `date`               | NULL     | none        | `YYYY-MM-DD`.                             |
| `assigned_time` | `Time`          | `time without time zone` | NULL | none    | `HH:MM` / `HH:MM:SS`.                      |
| `deadline_date` | `Date`          | `date`               | NULL     | none        | `YYYY-MM-DD`.                             |
| `deadline_time` | `Time`          | `time without time zone` | NULL | none    | `HH:MM` / `HH:MM:SS`.                      |
| `reminder_date` | `Date`          | `date`               | NULL     | none        | `YYYY-MM-DD`; reminder must be before the deadline. |
| `reminder_time` | `Time`          | `time without time zone` | NULL | none    | `HH:MM` / `HH:MM:SS`; reminder must be before the deadline. |
| `status`        | `String`        | `varchar`            | NULL     | `'PENDING'` | One of `PENDING`, `IN_PROGRESS`, `COMPLETED`. |
| `progress`      | `Integer`       | `integer`            | NULL     | `0`         | `0`–`100`.                                 |

`String` has no length limit here, so it maps to unbounded `varchar`.

### Value rules (enforced in app, not by DB constraints)

- `status`: exactly `PENDING`, `IN_PROGRESS`, or `COMPLETED` (uppercase).
- `progress`: integer `0..100`.
- `task_description` is required for progressive task creation and is not
  silently invented when the employee has not provided it.
- `status = 'COMPLETED'` requires `progress = 100`.
- `progress > 0` implies status must be `IN_PROGRESS` or `COMPLETED`, never
  `PENDING`.
- `reminder_date` / `reminder_time` must form a moment that is strictly after
  "now" and at or before the deadline. If either reminder part is `NULL`, the
  reminder is considered not set.
- NULL means "not stated"; do not invent dates/times or substitute `CURRENT_DATE`.

## Tables: authentication and RBAC

These tables are application-internal. The SQL skill must not reference them.

### `roles`

| Column | Postgres type | Nullable | Notes |
| --- | --- | --- | --- |
| `id` | `integer` | NOT NULL | Primary key |
| `name` | `varchar` | NOT NULL | Unique: `ADMIN`, `MANAGER`, or `EMPLOYEE` |
| `description` | `varchar` | NULL | Human-readable purpose |

### `permissions`

| Column | Postgres type | Nullable | Notes |
| --- | --- | --- | --- |
| `id` | `integer` | NOT NULL | Primary key |
| `name` | `varchar` | NOT NULL | Unique permission string |
| `description` | `varchar` | NULL | Human-readable purpose |

Canonical permissions: `tasks.create`, `tasks.read`, `tasks.read_all`,
`tasks.update`, `tasks.update_all`, `tasks.delete`, `employees.read`,
`employees.manage`, `roles.manage`, `reports.read`.

| Role | Permissions |
| --- | --- |
| `ADMIN` | All canonical permissions |
| `MANAGER` | All task permissions, `employees.read`, `employees.manage`, `reports.read` |
| `EMPLOYEE` | `tasks.read`, `tasks.update` (assigned tasks and progress only) |

### `role_permissions`

Associates roles and permissions.

| Column | Postgres type | Nullable | Notes |
| --- | --- | --- | --- |
| `role_id` | `integer` | NOT NULL | FK to `roles.id`, cascade delete |
| `permission_id` | `integer` | NOT NULL | FK to `permissions.id`, cascade delete |

Primary key: `(role_id, permission_id)`.

### `employees`

| Column | Postgres type | Nullable | Notes |
| --- | --- | --- | --- |
| `id` | `integer` | NOT NULL | Primary key |
| `google_sub` | `varchar` | NULL | Unique Google subject, linked on first sign-in |
| `name` | `varchar` | NOT NULL | Display name |
| `email` | `varchar` | NOT NULL | Unique, lower-case verified Google email |
| `role_id` | `integer` | NOT NULL | FK to `roles.id` |
| `is_active` | `boolean` | NOT NULL | Inactive employees cannot sign in or refresh |
| `timezone` | `varchar(64)` | NOT NULL | IANA timezone, default `Asia/Kolkata`; used to resolve reminder wall-clock time |
| `created_at` | `timestamp` | NOT NULL | UTC |
| `updated_at` | `timestamp` | NOT NULL | UTC |

### `sessions`

Stores one rotating refresh-token session. Only the SHA-256 hash of the
refresh token is stored; the raw token exists only in the HttpOnly cookie.

| Column | Postgres type | Nullable | Notes |
| --- | --- | --- | --- |
| `id` | `varchar` | NOT NULL | Primary key, random token |
| `employee_id` | `integer` | NOT NULL | FK to `employees.id`, cascade delete |
| `refresh_token_hash` | `varchar` | NOT NULL | Unique SHA-256 hash |
| `expires_at` | `timestamp` | NOT NULL | UTC refresh expiry |
| `revoked_at` | `timestamp` | NULL | Revocation/rotation marker |
| `created_at` | `timestamp` | NOT NULL | UTC |
| `last_used_at` | `timestamp` | NULL | UTC |
| `user_agent` | `varchar` | NULL | Truncated client details |
| `ip_address` | `varchar` | NULL | Best-effort client address |

Reusing a revoked refresh token revokes every active session for that
employee. Access tokens are signed JWTs and are never stored server-side.


## Tables: reminders, notifications, and push

These tables are application-internal. The SQL skill must not reference them.
They are added by `0003_reminder_notifications` and are accessed through
`services/notification_service.py` and `services/push_service.py`.

### `notifications`

One reminder/delivery row per employee and task. The row is visible in the
in-app notification center even when push delivery fails or no device is
registered.

| Column | Postgres type | Nullable | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | `integer` | NOT NULL | autoincrement | Primary key, indexed |
| `employee_id` | `integer` | NOT NULL | none | FK to `employees.id`, cascade delete, indexed |
| `task_id` | `integer` | NOT NULL | none | FK to `tasks.id`, cascade delete, indexed |
| `title` | `varchar(255)` | NOT NULL | none | Notification title |
| `message` | `text` | NOT NULL | none | Notification body |
| `notification_type` | `varchar(64)` | NOT NULL | `TASK_REMINDER` | e.g. `TASK_REMINDER`, `DEADLINE_APPROACHING`, `TASK_OVERDUE`, `TASK_ASSIGNED` |
| `priority` | `varchar(16)` | NOT NULL | `NORMAL` | `LOW`, `NORMAL`, `HIGH`, `CRITICAL` (0004) |
| `voice_text` | `text` | NULL | none | Foreground TTS announcement text (0004) |
| `payload` | `jsonb`/`json` | NULL | `{}` | Structured metadata for UI/delivery (0004) |
| `is_read` | `boolean` | NOT NULL | `false` | Self-only read marker |
| `scheduled_at` | `timestamp` | NOT NULL | none | Naive UTC reminder instant, indexed |
| `created_at` | `timestamp` | NOT NULL | now (UTC) | Row creation time |
| `delivered_at` | `timestamp` | NULL | none | Last successful push delivery |
| `delivery_status` | `varchar(32)` | NOT NULL | `PENDING` | `PENDING`, `PROCESSING`, `SENT`, `FAILED`, `CANCELLED` |
| `retry_count` | `integer` | NOT NULL | `0` | Delivery attempts |
| `next_retry_at` | `timestamp` | NULL | none | Backoff deadline, indexed |
| `last_error` | `text` | NULL | none | Last bounded delivery error |
| `locked_at` | `timestamp` | NULL | none | Worker claim time for stale-claim recovery |

Unique key:
`(employee_id, task_id, notification_type, scheduled_at)` named
`uq_notifications_employee_task_type_scheduled`. It prevents duplicate
reminders for the same task and reminder instant.

### `push_subscriptions`

One browser/device Web Push subscription, owned by one employee.

| Column | Postgres type | Nullable | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | `integer` | NOT NULL | autoincrement | Primary key, indexed |
| `employee_id` | `integer` | NOT NULL | none | FK to `employees.id`, cascade delete, indexed |
| `endpoint` | `varchar(1024)` | NOT NULL | none | Globally unique push endpoint |
| `p256dh_key` | `varchar(512)` | NOT NULL | none | Browser public key |
| `auth_key` | `varchar(512)` | NOT NULL | none | Browser auth secret |
| `device_name` | `varchar(255)` | NULL | none | Optional browser/device label |
| `is_active` | `boolean` | NOT NULL | `true` | Expired subscriptions are deactivated |
| `created_at` | `timestamp` | NOT NULL | now (UTC) | Registration time |
| `updated_at` | `timestamp` | NOT NULL | now (UTC) | Last update time |
| `last_used_at` | `timestamp` | NULL | none | Last delivery attempt |

Unique key: `endpoint` (`uq_push_subscriptions_endpoint`).

Reminder and subscription reads are always scoped to the authenticated
employee id. A manager or administrator does not automatically see another
employee's personal notifications or devices.

## Table: `conversation_memory`

Mapped class: `database.models.ConversationMemory`.

This table belongs to the **application**, not to the SQL generation skill.
The skill still may only reference the `tasks` table; this table is read and
written through the ORM in `services/conversation_memory.py` only.

It stores the small, persistent memory of one conversation, so a follow-up
message ("also update its deadline") can be understood after a restart of the
backend.

| Column            | SQLAlchemy type | Postgres type        | Nullable | Default              | Constraints / notes                                    |
| ----------------- | --------------- | -------------------- | -------- | -------------------- | ------------------------------------------------------ |
| `id`              | `Integer`       | `integer`            | NOT NULL | autoincrement        | Primary key, indexed (`ix_conversation_memory_id`).     |
| `conversation_id` | `String`        | `varchar`            | NOT NULL | none                 | Identifies the conversation, indexed.                   |
| `user_id`         | `String`        | `varchar`            | NOT NULL | none                 | Identifies the user, indexed. Authenticated requests use `employee:<id>`. |
| `employee_id`     | `Integer`       | `integer`            | NULL     | none                 | FK to `employees.id`, set from the authenticated actor. |
| `active_task_id`  | `Integer`       | `integer`            | NULL     | none                 | The task the employee is working on, indexed.           |
| `last_action`     | `String`        | `varchar`            | NULL     | none                 | The last action that was carried out successfully.      |
| `context`         | `JSON`/`JSONB`  | `jsonb`              | NULL     | `{}`                 | Snapshot of the active task and the last turns.        |
| `response_feedback` | `Boolean`     | `boolean`            | NULL     | `NULL`               | `TRUE` thumbs up, `FALSE` thumbs down, `NULL` no rating. |
| `updated_at`      | `DateTime`      | `timestamp without time zone` | NOT NULL | now (UTC)  | Written on every change.                               |

- `UNIQUE (user_id, conversation_id)` - there is exactly one memory per user
  and conversation (`uq_conversation_memory_user_conversation`).
- Authenticated chat and voice turns set `user_id` to `employee:<employee id>`
  and fill `employee_id` from the request-scoped actor. Client-supplied
  `user_id` values are ignored when authentication is enabled.
- The `context` column holds a small JSON structure, never a full transcript:

  ```json
  {
    "active_task": {"id": 211, "task_title": "Business report",
                    "task_description": "Monthly business report", "assigned_to": "Rahul",
                    "status": "IN_PROGRESS", "progress": 10, "deadline_date": "2026-11-21"},
    "history": [
      {"role": "user", "text": "...", "at": "2026-11-13T09:30:00"},
      {"role": "assistant", "text": "...", "at": "2026-11-13T09:30:01"}
    ],
    "pending": {"instruction": "Update the progress of the business report to 10%",
                "changes": {"progress": 10}, "find": {}, "task_id": null},
    "candidates": [{"id": 211, "task_title": "Business report"}],
    "clarification": {"stage": "select_task", "action": "update",
                      "intent": "CLARIFICATION_REQUIRED",
                      "changes": {"progress": 10}, "find": {}, "task_id": null}
  }
  ```

- `task_creation` holds the persisted progressive-create clarification:
  `stage`, the collected `draft`, `missing_fields`, `last_question` and the
  fields already collected. It survives a backend restart and is cleared once
  the task is saved or the employee starts another operation.
- The history keeps the last 10 messages, each with a UTC timestamp (`at`).
  It is read newest-first, limited to 10, then sent to the model in
  chronological order.
- `pending` holds the change the employee asked for while the task of the
  change is still unknown. It is cleared as soon as the change is applied.
- `candidates` keeps the task candidates that were displayed (at most 10), so
  "the second one" or "the previous one" can still be resolved after a
  restart.
- `clarification` keeps the stage and the draft of a waiting clarification.
  It is cleared together with `pending` after a successful operation or when
  the employee starts a different operation.
- `response_feedback` stores the latest thumbs up (`TRUE`) or thumbs down
  (`FALSE`) of the conversation. It is only changed by the feedback endpoint;
  new messages and history updates never clear it.

### Verification queries

```sql
-- every stored memory
SELECT id, user_id, conversation_id, active_task_id, last_action, updated_at
FROM conversation_memory
ORDER BY updated_at DESC;

-- the memory of one conversation
SELECT context FROM conversation_memory
WHERE user_id = 'user-1' AND conversation_id = 'conversation-1';

-- the active task of every conversation
SELECT user_id, conversation_id, active_task_id, last_action
FROM conversation_memory
WHERE active_task_id IS NOT NULL
ORDER BY updated_at DESC;

-- the conversations of one user
SELECT conversation_id, active_task_id, updated_at
FROM conversation_memory
WHERE user_id = 'user-1'
ORDER BY updated_at DESC;

-- the memory of a task that is still active
SELECT count(*) FROM conversation_memory WHERE active_task_id = 211;

-- the stored turns of one conversation, oldest first
SELECT turn->>'role' AS role, turn->>'text' AS text, turn->>'at' AS at
FROM conversation_memory,
     jsonb_array_elements(context->'history') AS turn
WHERE user_id = 'user-1' AND conversation_id = 'conversation-1'
ORDER BY turn->>'at';
```

## Query generation rules

1. Only query the `tasks` table; there are no joins or other tables.
2. Use the exact snake_case column names above. Quote only if needed.
3. Prefer explicit column lists over `SELECT *` in generated SQL.
4. For natural-language answers, add `ORDER BY` so results are deterministic
   (default: `ORDER BY id`).
5. Use parameter placeholders supported by the executor (`$1`, `$2`, ... for
   psycopg, or `:name` for SQLAlchemy) instead of string interpolation when
   generating queries that take user values. Never inline untrusted input.
6. Read requests -> `SELECT`. Write requests -> `INSERT`, `UPDATE`, `DELETE`,
   or `DROP TABLE tasks` per the Guardrails. `UPDATE` and `DELETE` always
   include a `WHERE` clause. Destructive operations require explicit
   confirmation before execution.
7. Date/time output should use `to_char(<date_column>, 'YYYY-MM-DD')` and
   `to_char(<time_column>, 'HH24:MI')` when formatting for display, applying
   this to assigned, deadline, and reminder date/time columns.
8. Case-insensitive text search: `ILIKE '%term%'`. A multi-word search is
   split into concepts (`database/search.py`) and each concept becomes an
   `ILIKE` parameter joined with `OR`; the returned rows are ranked by
   relevance. The title, the assignee and the status are searchable.
9. Progress/status filters must use the canonical uppercase status values.
10. An ownership scope is an exact, case-insensitive match
    (`assigned_to ILIKE :owner`, no wildcards), never a partial match.
    The authenticated application additionally enforces ownership with
    `assigned_to_employee_id`; display names are never the authorization
    mechanism for signed-in employees.

## Common query templates

All tasks (newest first):

```sql
SELECT id, task_title, task_description, assigned_to, assigned_date,
       assigned_time, deadline_date, deadline_time, reminder_date,
       reminder_time, status, progress
FROM tasks
ORDER BY id DESC;
```

Single task by id:

```sql
SELECT id, task_title, task_description, assigned_to, assigned_date,
       assigned_time, deadline_date, deadline_time, reminder_date,
       reminder_time, status, progress
FROM tasks
WHERE id = $1;
```

Filter by assignee (case-insensitive):

```sql
SELECT id, task_title, status, progress
FROM tasks
WHERE assigned_to ILIKE $1
ORDER BY id;
```

Filter by status and progress:

```sql
SELECT id, task_title, assigned_to, progress
FROM tasks
WHERE status = 'IN_PROGRESS' AND progress BETWEEN $1 AND $2
ORDER BY progress DESC, id;
```

Search by title/assignee:

```sql
SELECT id, task_title, assigned_to, status
FROM tasks
WHERE task_title ILIKE $1 OR assigned_to ILIKE $1
ORDER BY id;
```

Tasks due on a date / overdue:

```sql
SELECT id, task_title, assigned_to, deadline_date, status, progress
FROM tasks
WHERE deadline_date = $1
ORDER BY deadline_time NULLS LAST, id;

SELECT id, task_title, assigned_to, deadline_date, status, progress
FROM tasks
WHERE deadline_date < CURRENT_DATE
  AND status <> 'COMPLETED'
ORDER BY deadline_date, id;
```

Tasks with a reminder on a date / upcoming reminders:

```sql
SELECT id, task_title, assigned_to, deadline_date, deadline_time,
       reminder_date, reminder_time, status, progress
FROM tasks
WHERE reminder_date = $1
ORDER BY reminder_time NULLS LAST, id;

SELECT id, task_title, reminder_date, reminder_time, deadline_date, deadline_time
FROM tasks
WHERE reminder_date IS NOT NULL
  AND (
      reminder_date > CURRENT_DATE
      OR (reminder_date = CURRENT_DATE AND reminder_time > CURRENT_TIME)
  )
ORDER BY reminder_date, reminder_time, id;
```

Insert a task:

```sql
INSERT INTO tasks (
    task_title, task_description, assigned_to, assigned_date, assigned_time,
    deadline_date, deadline_time, reminder_date, reminder_time,
    status, progress
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING id, task_title, task_description, reminder_date, reminder_time,
          status, progress;
```

Update progress/status by id:

```sql
UPDATE tasks
SET progress = $1, status = $2
WHERE id = $3
RETURNING id, task_title, status, progress;
```

Delete by id (requires confirmation):

```sql
DELETE FROM tasks
WHERE id = $1
RETURNING id, task_title, assigned_to, status, progress;
```

Drop table (requires confirmation and an explicit warning):

```sql
DROP TABLE tasks;
```

## Answering from results

- Empty result set -> say no matching tasks were found; do not fabricate rows.
- Report `NULL` date/time fields (including `reminder_date` / `reminder_time`)
  as "not set" rather than `null`.
- For counts, use `COUNT(*)`, e.g.
  `SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status;`

## How the application uses this schema

The API never runs a statement that a model wrote. The runtime path is:

1. `ai/intent_detection.py` classifies the employee message.
2. `ai/text_extractor.py` reads only the values that were stated.
3. `database/sql_generator.py` derives the statement from the ORM model in
   `database/models.py` and returns a structured operation
   (`intent`, `operation`, `table`, `task_id`, `updates`, `sql`, `params`).
4. `database/sql_generator.validate_operation` and
   `database/sql_execution.validate_sql` check the operation and the
   statement.
5. `database/sql_execution.execute_operation` runs it through
   `SqlAlchemyConnection`, which binds every value as a parameter.
6. `database/task_repository.py` re-reads the row and the service serialises
   it for the response.

Consequences for the statements above:

- The `WHERE` clause of an `UPDATE` always contains `id = :task_id` and, when
  the employee is known, `assigned_to = :assigned_to`.
- Values are always bound; a value that looks like SQL (`'; DROP TABLE
  tasks; --`) is stored as plain text.
- `INSERT` is only used for an explicit task creation; an update never
  inserts a row.
- `DELETE` needs the exact phrase `CONFIRM DELETE` before it runs.
