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
- Tables are created by `Base.metadata.create_all()` in `create_tables()`.
  There are no migrations and no foreign keys.

## Table: `tasks`

Mapped class: `database.models.Task`.

| Column          | SQLAlchemy type | Postgres type        | Nullable | Default     | Constraints / notes                       |
| --------------- | --------------- | -------------------- | -------- | ----------- | ----------------------------------------- |
| `id`            | `Integer`       | `integer`            | NOT NULL | autoincrement | Primary key, indexed (`ix_tasks_id`).     |
| `task_title`    | `String`        | `varchar`            | NOT NULL | none        | Required. Use non-empty text.             |
| `assigned_to`   | `String`        | `varchar`            | NULL     | none        | Person name or `NULL`.                    |
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
- `status = 'COMPLETED'` requires `progress = 100`.
- `progress > 0` implies status must be `IN_PROGRESS` or `COMPLETED`, never
  `PENDING`.
- `reminder_date` / `reminder_time` must form a moment that is strictly after
  "now" and at or before the deadline. If either reminder part is `NULL`, the
  reminder is considered not set.
- NULL means "not stated"; do not invent dates/times or substitute `CURRENT_DATE`.

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
| `user_id`         | `String`        | `varchar`            | NOT NULL | none                 | Identifies the user, indexed.                           |
| `active_task_id`  | `Integer`       | `integer`            | NULL     | none                 | The task the employee is working on, indexed.           |
| `last_action`     | `String`        | `varchar`            | NULL     | none                 | The last action that was carried out successfully.      |
| `context`         | `JSON`/`JSONB`  | `jsonb`              | NULL     | `{}`                 | Snapshot of the active task and the last turns.        |
| `updated_at`      | `DateTime`      | `timestamp without time zone` | NOT NULL | now (UTC)  | Written on every change.                               |

- `UNIQUE (user_id, conversation_id)` - there is exactly one memory per user
  and conversation (`uq_conversation_memory_user_conversation`).
- The `context` column holds a small JSON structure, never a full transcript:

  ```json
  {
    "active_task": {"id": 211, "task_title": "Business report", "assigned_to": "Rahul",
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

## Common query templates

All tasks (newest first):

```sql
SELECT id, task_title, assigned_to, assigned_date, assigned_time,
       deadline_date, deadline_time, reminder_date, reminder_time,
       status, progress
FROM tasks
ORDER BY id DESC;
```

Single task by id:

```sql
SELECT id, task_title, assigned_to, assigned_date, assigned_time,
       deadline_date, deadline_time, reminder_date, reminder_time,
       status, progress
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
    task_title, assigned_to, assigned_date, assigned_time,
    deadline_date, deadline_time, reminder_date, reminder_time,
    status, progress
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING id, task_title, reminder_date, reminder_time, status, progress;
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
