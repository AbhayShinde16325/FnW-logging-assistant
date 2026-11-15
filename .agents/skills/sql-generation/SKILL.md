
---
name: SQL Generation (FnW tasks)
description: Generate and safely execute PostgreSQL for the FnW tasks table, with explicit confirmation before UPDATE, DELETE, or DROP TABLE.
---

# SQL Generation (FnW tasks)

This skill is provider-independent. It works with any LLM agent that can read
`DATABASE_SCHEMA.md` and call a JSON-shaped execution layer.

## 1. Read the schema first

Always read `DATABASE_SCHEMA.md` before generating SQL.

If the file is missing, stop and ask for the schema.

Do not invent tables, columns, or value rules.

## 2. Classify the request

Classify the user request into one of:

- `SELECT`
- `INSERT`
- `UPDATE`
- `DELETE`
- `DROP TABLE`
- `CLARIFY`
- `REFUSE`

If the request is ambiguous, ask one clarifying question and generate no SQL.

For retrieval requests, additionally identify the requested information:

- Task summary
- Task status
- Task progress
- Task reminder date
- Task deadline
- Task title
- Task assignee
- Task creation date
- Task start date
- Any other existing task field

A request for one specific field must be treated as a field-specific retrieval request, not a general task summary request.

## 3. Input contract

A structured request looks like this:

```json
{
  "operation": "DELETE",
  "sql": "DELETE FROM tasks WHERE id = :id",
  "params": {"id": 7},
  "confirmation": null
}
```

Rules:

- `operation` is optional, but when present it must match the SQL.
- `sql` is a single SQL statement.
- `params` is a dict or list of parameter values.
- `confirmation` is either `null` or the exact confirmation phrase.

## 4. Output contract

A structured result looks like this:

```json
{
  "status": "confirmation_required",
  "operation": "DELETE",
  "sql": "DELETE FROM tasks WHERE id = :id",
  "params": {"id": 7},
  "target_table": "tasks",
  "impact": "Permanently deletes the rows in tasks matched by WHERE.",
  "confirmation_phrase": "CONFIRM DELETE",
  "message": "Explicit confirmation is required before executing this statement."
}
```

Statuses:

- `success`
- `confirmation_required`
- `clarification_required`
- `error`

For successful SELECT requests, return the actual retrieved database records and the requested fields.

Do not replace a field-specific result with a generic task summary.

## 5. Operation policy

| Operation | Allowed | Confirmation | Extra rules |
|---|---|---|---|
| `SELECT` | Yes | None | `tasks` only; explicit columns preferred. |
| `INSERT` | Yes | None | Parameterized; follow schema value rules. |
| `UPDATE` | Yes | Required | Specific `WHERE` clause required. |
| `DELETE` | Yes | Required | Specific, non-tautological `WHERE` clause required. |
| `DROP TABLE tasks` | Yes | Required | Exact table only; no `CASCADE`; no multiple tables. |

## 6. Confirmation phrases

Destructive operations require exactly one of these phrases:

- `CONFIRM UPDATE`
- `CONFIRM DELETE`
- `CONFIRM DROP TABLE tasks`

A confirmation is valid only when it matches the operation exactly.

Bare agreement such as `yes`, `ok`, `sure`, `go ahead`, or `do it` is not valid confirmation.

## 7. Workflow

1. Read `DATABASE_SCHEMA.md`.
2. Classify the request.
3. If ambiguous, ask one clarification question and stop.
4. If prohibited, refuse and explain why.
5. Generate parameterized SQL that references only `tasks`.
6. For `SELECT` and `INSERT`, execute without confirmation.
7. For `UPDATE`, `DELETE`, or `DROP TABLE tasks`, return a `confirmation_required` result with the exact SQL, parameters, target table, impact, and required confirmation phrase.
8. Only after the user supplies the exact phrase, execute the statement.
9. Report the outcome without inventing rows or success that did not happen.

## 8. Execution layer

Prefer the shared execution layer `database.sql_execution.run_sql_request`.

It enforces the same rules in code:

```python
from database.sql_execution import run_sql_request

result = run_sql_request(
    {
        "operation": "DELETE",
        "sql": "DELETE FROM tasks WHERE id = :id",
        "params": {"id": 7},
        "confirmation": None,
    },
    connection=connection,
)
```

If the host does not expose that helper, follow the contract above and do not execute destructive SQL without explicit confirmation.

## 9. Parameterization

Use placeholders supported by the executor, for example `:id` or `$1`.

Never inline untrusted values into SQL.

## 10. Prohibited operations

Refuse and explain instead of generating SQL for:

- `TRUNCATE`
- `ALTER`
- `CREATE TABLE`
- `CREATE DATABASE`
- Any table other than `tasks`
- Joins
- Multiple statements in one request

## 11. Ambiguity

If the user says something like "delete the old ones" or "clear task 7's reminder" without enough detail, ask which field, row, or filter they mean.

Do not guess.

For task retrieval:

- If multiple tasks match, ask the user to identify the exact task.
- If the previous conversation has an unambiguous task ID, reuse that task ID.
- If the user says "its reminder date", "its deadline", "its progress", or similar, resolve "its" using the active task in conversation memory.
- Do not search for a new task when the active task is already known.
- If no active task can be resolved, ask for the task ID or exact title.

## 12. Runtime integration (the FastAPI application)

This skill is also implemented in Python, so the running application obeys the same rules instead of relying on the agent to remember them.

| Concern | Module |
|---|---|
| Intent detection | `ai/intent_detection.py` |
| Structured extraction | `ai/text_extractor.py` |
| Conversational state | `ai/query_session.py` |
| Business operations | `services/task_service.py` |
| SQL generation | `database/sql_generator.py` |
| SQL validation and execution | `database/sql_execution.py` |
| Database access | `database/task_repository.py` |

The application never executes a statement that a model wrote.

It builds a structured operation, validates it, and lets the execution layer turn it into a parameterized statement:

```python
{
    "intent": "UPDATE_TASK",
    "operation": "UPDATE",
    "table": "tasks",
    "task_id": 12,
    "owner": "Rahul",
    "updates": {
        "status": "IN_PROGRESS",
        "progress": 70
    },
    "sql": "UPDATE tasks SET status = :status, progress = :progress "
          "WHERE id = :task_id AND assigned_to = :assigned_to",
    "params": {
        "status": "IN_PROGRESS",
        "progress": 70,
        "task_id": 12,
        "assigned_to": "Rahul"
    }
}
```

Rules that the runtime enforces in code:

1. `database.sql_generator.build_*_operation` derives the table, the columns and the placeholders from `database/models.py`; the model can only fill in values.
2. `database.sql_generator.validate_operation` rejects unknown columns, invalid values, another table, a missing task ID and a statement that does not match the operation.
3. `database.sql_execution.validate_sql` re-checks the statement itself (single statement, `tasks` only, a specific `WHERE`, no joins).
4. `database.sql_execution.execute_operation` runs the statement through `SqlAlchemyConnection`, which binds the parameters and commits the transaction.
5. The service re-reads the row afterwards and serializes it for the caller.

### Confirmation in the application

The confirmation phrases stay the same.

In the conversational flow the phrase is granted by the application only when the employee explicitly asked for the change and the change set is not empty:

- `UPDATE` – granted for an explicit update request, and for a staged update the employee already asked for in an earlier turn.
- `DELETE` – never granted automatically. The employee has to send the exact phrase `CONFIRM DELETE` (or the client has to send `confirm_delete`).

### Intents

`ai/intent_detection.py` classifies a message as:

- `CREATE_TASK`
- `UPDATE_TASK`
- `GET_TASK`
- `LIST_TASKS`
- `DELETE_TASK`
- `GENERAL_QUERY`
- `CLARIFICATION_REQUIRED`

An update request never starts a new task.

## 13. Direct task field retrieval (IMPORTANT)

This section defines how the agent must answer questions about individual task fields.

### 13.1 Identify the requested field

When the user asks a question about an existing task, identify the exact field requested.

Use the following semantic mappings:

| User asks | Requested information |
|---|---|
| "What is its progress?" | `progress` |
| "How much is completed?" | `progress` |
| "What percentage is completed?" | `progress` |
| "What is its status?" | `status` |
| "Is it pending or in progress?" | `status` |
| "What is its reminder date?" | The actual reminder-date column from the schema |
| "When should I be reminded?" | The actual reminder-date column from the schema |
| "What is its deadline?" | The actual deadline column from the schema |
| "When is it due?" | The actual deadline column from the schema |
| "Who is assigned to it?" | The actual assignee column from the schema |
| "What is its title?" | The actual task-title column from the schema |

The mappings above represent user intent, not permission to invent database column names.

Always verify the real column names in `DATABASE_SCHEMA.md` and `database/models.py`.

### 13.2 Retrieve only the required field

For a question about one field, retrieve only that field whenever practical.

For example, if the user asks for task #1784's progress, generate a SELECT using the actual progress column:

```sql
SELECT id, progress
FROM tasks
WHERE id = :task_id;
```

Parameters:

```python
{"task_id": 1784}
```

For a reminder-date question, use the actual reminder-date column confirmed by the schema:

```sql
SELECT id, <actual_reminder_date_column>
FROM tasks
WHERE id = :task_id;
```

For a deadline question, use the actual deadline column confirmed by the schema:

```sql
SELECT id, <actual_deadline_column>
FROM tasks
WHERE id = :task_id;
```

The angle-bracket placeholders above are illustrative only. Never execute them as SQL.

Do not retrieve a new list of matching tasks if the task ID is already known from conversation memory.

### 13.3 Direct answer policy

The response must contain the requested value immediately.

Examples:

User:

"What is the current progress?"

Correct:

"40% completed."

Incorrect:

"Task #1784 — Business report pytest-a0f5f4e8b013 is PENDING at 0%."

User:

"What is its current reminder date?"

Correct:

"20 November 2026."

Incorrect:

"Task #1784 — Business report pytest-a0f5f4e8b013 is PENDING at 0%."

User:

"What is its deadline?"

Correct:

"25 November 2026."

Incorrect:

"Your task is currently pending. Here are its details..."

The example dates and progress values are illustrative. Always use the actual retrieved database values.

### 13.4 Response formatting rules

Follow these rules:

1. Answer the exact question first.
2. Return one field's value when one field was requested.
3. Do not return the entire task object unless the user asks for full task details.
4. Do not repeat the task title, ID, owner, status, or description unnecessarily.
5. Do not add explanations that the user did not request.
6. Format dates consistently as `DD Month YYYY`.
7. Display progress with a percentage sign.
8. Display status using its actual stored value.
9. If the field is NULL, say that no value is set.
10. Never fabricate or infer a missing deadline or reminder date.
11. Do not confuse the system's current date with the task's deadline or reminder date.
12. Do not treat the last update timestamp as the reminder date.

### 13.5 Contextual follow-up handling

Conversation memory should preserve:

- `active_task_id`
- `active_task_title`
- `last_requested_field`
- `last_action`
- Relevant conversation history

Example conversation:

User: "Get task 1784."

Agent: "Task #1784 found."

User: "What is its progress?"

Agent: "40% completed."

User: "What is its reminder date?"

Agent: "20 November 2026."

User: "What is its deadline?"

Agent: "25 November 2026."

User: "And its status?"

Agent: "PENDING."

Each question must retrieve the corresponding field for the same active task.

The values in this example are illustrative and must not be reused as actual task data.

### 13.6 Follow-up query rewriting

`services/query_rewriter.py` must resolve references such as:

- its
- that task
- this task
- the same task
- its date
- its progress
- its deadline
- its reminder

For example:

Original message:

"What is its reminder date?"

Rewritten structured request:

```json
{
    "intent": "GET_TASK",
    "task_id": 1784,
    "requested_field": "reminder_date"
}
```

`reminder_date` is a semantic field identifier here. The repository must map it to the actual database column after schema validation.

The rewriter must not:

- Invent a task ID.
- Invent a database field.
- Invent a date.
- Replace a known active task with another task.
- Execute SQL.
- Modify database values.

### 13.7 Repository and service response

The retrieval flow should follow this sequence:

1. Resolve the active task ID.
2. Resolve the requested field.
3. Validate the field against the model/schema.
4. Retrieve the latest database value.
5. Serialize the requested field.
6. Return the value directly to the conversational response layer.

The repository remains the only module responsible for database access.

The service must preserve the retrieved field in its response instead of dropping it and returning only a generic task summary.

### 13.8 Missing and NULL values

If the requested field does not exist in the schema:

"The requested field is not available in the task database."

If the field exists but its value is NULL:

"No reminder date has been set for this task."

If the task ID does not exist:

"Task #1784 was not found."

If the active task is ambiguous:

"Which task are you referring to? Please provide its task ID."

Never substitute a different field's value.

### 13.9 Field-specific SELECT examples

These examples are conceptual. Actual columns must be verified against `DATABASE_SCHEMA.md`.

Progress:

```sql
SELECT id, progress
FROM tasks
WHERE id = :task_id;
```

Status:

```sql
SELECT id, status
FROM tasks
WHERE id = :task_id;
```

Reminder date:

```sql
SELECT id, <actual_reminder_date_column>
FROM tasks
WHERE id = :task_id;
```

Deadline:

```sql
SELECT id, <actual_deadline_column>
FROM tasks
WHERE id = :task_id;
```

Task title:

```sql
SELECT id, <actual_task_title_column>
FROM tasks
WHERE id = :task_id;
```

All retrieval operations must use parameterized queries and the existing execution layer.

## 14. Direct response examples

| User question | Expected response |
|---|---|
| What is the progress? | 40% completed. |
| What is the status? | PENDING. |
| What is its reminder date? | 20 November 2026. |
| What is its deadline? | 25 November 2026. |
| Who is assigned to it? | Rahul. |
| What is its title? | Business report. |
| Show full task details. | Return the full task details. |

All values above are examples, not hardcoded responses.

The response layer must use the actual result returned by the database.

## 15. Date/time handling

- Use `datetime_resolver.resolve(text, now=..., timezone=...)` for natural-language dates and times.
- Do not duplicate relative-date parsing.
- Do not substitute `CURRENT_DATE` for a missing or unstated date.
- In tests, pass a fixed `now` and timezone for deterministic results.
- When retrieving a stored reminder date or deadline, return the stored value. Do not resolve it again using the current date.
- Distinguish between a task's reminder date, deadline, creation date, and the current system date.
- Do not calculate or infer a task's deadline from its reminder date.
- Do not overwrite a stored date when the user is only asking to retrieve it.
