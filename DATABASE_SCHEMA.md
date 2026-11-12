# Database Schema Reference (SQL)

Reference for generating SQL against the Task Flow Automation database.
Source of truth: `database/models.py`, `database/connection.py`,
`database/task_repository.py`. If the ORM model changes, update this file.

## Guardrails (MUST follow)

These guardrails are non-negotiable and apply to every generated query.

- The LM may only generate SQL for **read (SELECT)**, **create (INSERT)**, and
  **update (UPDATE)** operations.
- The LM MUST NOT generate, suggest, or execute **delete operations** —
  including `DELETE`, `TRUNCATE`, `DROP`, or any other destructive statement.
- If a user asks to delete data, refuse the SQL generation and respond that
  delete operations are not permitted. Do not provide a workaround query.
- Only the `tasks` table may be referenced. No other tables, no joins, no
  schema changes (`ALTER`, `CREATE TABLE`, etc.).
- Every `UPDATE` MUST include a `WHERE` clause; never generate a query that
  updates all rows unconditionally.
- This reference and the rules below apply only to `SELECT`, `INSERT`, and
  `UPDATE`.

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

## Query generation rules

1. Only query the `tasks` table; there are no joins or other tables.
2. Use the exact snake_case column names above. Quote only if needed.
3. Prefer explicit column lists over `SELECT *` in generated SQL.
4. For natural-language answers, add `ORDER BY` so results are deterministic
   (default: `ORDER BY id`).
5. Use parameter placeholders (`$1`, `$2`, ... for psycopg) instead of string
   interpolation when generating queries that take user values. Never inline
   untrusted input.
6. Read requests -> `SELECT`. Write requests -> `INSERT` / `UPDATE` only.
   `DELETE` is not permitted (see Guardrails). For `UPDATE` always include a
   `WHERE` clause.
7. Date/time output should use `to_char(<date_column>, 'YYYY-MM-DD')` and
   `to_char(<time_column>, 'HH24:MI')` when formatting for display, applying
   this to assigned, deadline, and reminder date/time columns.
8. Case-insensitive text search: `ILIKE '%term%'`.
9. Progress/status filters must use the canonical uppercase status values.

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

Delete by id: **PROHIBITED — do not generate.** The LM must not produce
`DELETE` (or `TRUNCATE`/`DROP`) statements. Refuse delete requests instead.

## Answering from results

- Empty result set -> say no matching tasks were found; do not fabricate rows.
- Report `NULL` date/time fields (including `reminder_date` / `reminder_time`)
  as "not set" rather than `null`.
- For counts, use `COUNT(*)`, e.g.
  `SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status;`
