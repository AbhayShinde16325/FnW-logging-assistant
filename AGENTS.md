
# Agent instructions

- Read `DATABASE_SCHEMA.md` before generating or executing any SQL.
- Detailed SQL workflow: `.agents/skills/sql-generation/SKILL.md`.
- Only the `tasks` table may be referenced.
- `SELECT` and `INSERT` require no confirmation.
- `UPDATE`, `DELETE`, and `DROP TABLE tasks` require explicit confirmation before execution.
- Exact confirmation phrases: `CONFIRM UPDATE`, `CONFIRM DELETE`, `CONFIRM DROP TABLE tasks`.
- Never execute destructive SQL before confirmation.
- `DELETE` must always include a specific `WHERE` clause.
- `DROP TABLE tasks` must name the exact table and warn that the whole table and all rows will be removed.
- Prohibited: `TRUNCATE`, `ALTER`, `CREATE DATABASE`, and unrelated operations.
- Use parameterized SQL; never inline untrusted input.
- Ask a clarification question instead of guessing when a request is ambiguous.
- Prefer the shared execution layer `database.sql_execution` when available; it enforces the same confirmation rules in code.

## Runtime integration

The SQL skill is implemented in Python as well, so the running API obeys the same rules:

- `ai/tool_calling.py` wraps the existing provider's native function/tool calling. It returns text, tool calls and a plan; it never touches the database.
- `services/database_agent.py` is the LLM-controlled loop. The model receives the live schema, conversation context and tool contracts, chooses a tool, observes the result and decides what to do next. It never routes on hardcoded intent keywords.
- Agent tools: `inspect_database_schema`, `inspect_table_definition`, `validate_sql_query`, `execute_read_query`, `execute_write_query`, `retrieve_conversation_context`, `update_conversation_context`.
- `database/sql_execution.py` validates every model-generated statement (one statement, only `tasks`, no joins, no prohibited operations, specific `WHERE` for writes, exact confirmation for destructive statements) and binds every value.
- `database/task_repository.py` executes reads in a read-only transaction with a statement timeout and a row cap; it is the only module that talks to PostgreSQL.
- `database/sql_generator.py` and `database/read_planner.py` remain the deterministic fallback path (structured operations and schema-aware plans).
- `ai/intent_detection.py`, `services/agentic_read.py`, `services/query_rewriter.py` and the staged flows remain as the fallback when `DATABASE_AGENT` is off or the agent cannot start.
- `services/task_service.py` selects the agent for fresh chat/voice turns and keeps the existing state machine for staged turns; `app.py` only routes HTTP.

The model may generate SQL, but it never executes it directly. Every statement passes `validate_sql` first, reads run read-only and bounded, employee-scoped reads must return `assigned_to` and are filtered to the employee, and employee-scoped writes must carry an `assigned_to` predicate with the employee value bound. The confirmation phrase is taken from the employee's actual message, never from the model. A model may not invent a task ID or a value outside the live schema.

Enable the agent with `DATABASE_AGENT=1` (Compose default). The deterministic flows handle any turn the agent cannot start, and staged clarification answers stay in the existing state machine.

"Pending" in ordinary language means open work (`PENDING` and `IN_PROGRESS`). Use the exact `PENDING` value only when the employee says "status is pending" or otherwise refers to the stored status. `database/read_planner.py` owns this rule.

See section 12 of `.agents/skills/sql-generation/SKILL.md`.

## Conversation memory

- `database/models.py` also maps `conversation_memory` (one row per user and conversation). It is an application-internal table: the SQL skill still only references `tasks`.
- `services/conversation_memory.py` reads and writes that table through the ordinary SQLAlchemy session.
- `services/query_rewriter.py` rewrites a follow-up message into a standalone instruction. It never executes anything and never invents a task ID or a value.
- The rewriting happens in `services/task_service.py` before the existing agent flow runs, so the validation and the authorisation are unchanged.

## Date/time handling

- Use `datetime_resolver.resolve(text, now=..., timezone=...)` for natural-language dates and times.
- Do not duplicate relative-date parsing.
- Do not substitute `CURRENT_DATE` for a missing or unstated date.
- In tests, pass a fixed `now` and timezone for deterministic results.

## Direct task field retrieval (IMPORTANT)

When an employee asks about a specific task field, return the requested value directly.

Do not return a generic task summary when the user asks for one specific field.

### 1. Direct answer requirements

Examples:

| User asks | Expected response |
|---|---|
| What is the current progress? | 40% completed. |
| How much is completed? | 40% completed. |
| What is its status? | PENDING. |
| What is its reminder date? | 20 November 2026. |
| What is its exact deadline? | 25 November 2026. |
| Who is assigned to it? | Rahul. |
| What is its title? | Business report. |

These are illustrative values. Always retrieve actual values from PostgreSQL.

### 2. Retrieve the requested field

- Detect which specific task field the user is asking about.
- Resolve the task ID from the current message or conversation memory.
- Retrieve the latest value from PostgreSQL.
- Return only the requested field.
- Do not return the complete task object unless explicitly requested.
- Do not use old conversational values when a fresh database retrieval is available.
- Do not substitute the task's status or summary for a requested reminder date, deadline, or progress.

### 3. Contextual follow-ups

The agent must understand follow-up messages without requiring the user to repeat the task ID.

Example:

User: Get task 1784.

Agent: Task #1784 found.

User: What is its current progress?

Agent: 40% completed.

User: What is its reminder date?

Agent: 20 November 2026.

User: What about its deadline?

Agent: 25 November 2026.

User: And its status?

Agent: PENDING.

The task ID must remain 1784 throughout this conversation unless the user explicitly selects another task.

### 4. Semantic field identification

Understand natural-language variations:

- Progress: progress, completion, percentage completed, how much is done.
- Status: status, state, pending, in progress, completed.
- Reminder date: reminder, reminder date, notification date, when should I be reminded.
- Deadline: deadline, due date, submission date, when is it due.
- Assignee: assigned person, owner, who is responsible.
- Title: task name, title, what is the task.

Map these concepts to actual schema fields. Never assume a database column exists without checking the schema.

### 5. Response formatting

- Progress must be displayed as a percentage.
- Dates must be displayed in `DD Month YYYY` format where applicable.
- Status must use the actual stored value.
- Return a short, direct answer.
- If the value is NULL, explicitly say it has not been set.
- If the task does not exist, say that it was not found.
- If a requested field is unavailable in the schema, explain that it is not available.
- Never invent a value.

### 6. Separate reminder dates and deadlines

Reminder dates and deadlines are different task properties.

- A reminder date tells the employee when they should receive a reminder.
- A deadline tells the employee when the task must be completed.
- Never use one as a substitute for the other.
- Never return `updated_at` as the reminder date.
- Never calculate a deadline from a reminder date.
- Retrieve the exact stored value of the requested property.

### 7. Structured retrieval request

For a follow-up question such as:

"What is its reminder date?"

The rewritten request should look conceptually like:

```json
{
    "intent": "GET_TASK",
    "task_id": 1784,
    "requested_field": "reminder_date"
}
```

For a progress question:

```json
{
    "intent": "GET_TASK",
    "task_id": 1784,
    "requested_field": "progress"
}
```

For a deadline question:

```json
{
    "intent": "GET_TASK",
    "task_id": 1784,
    "requested_field": "deadline"
}
```

These are semantic field identifiers, not necessarily database column names.

The application must map them to validated schema fields before generating SQL.

### 8. Retrieval flow

The expected flow is:

1. Detect `GET_TASK`.
2. Identify the active task ID.
3. Identify the requested field.
4. Validate the field against `database/models.py`.
5. Call `database/task_repository.py` to retrieve the latest value.
6. Return the requested field through `services/task_service.py`.
7. Format the response directly.

Do not stop after retrieving the task's general summary.

Do not discard the requested field during response serialization.

### 9. Preserve existing functionality

These changes must not break:

- Task creation.
- Task updates.
- Task deletion.
- Task listing.
- Multiple-task clarification.
- Task assignment.
- Progress updates.
- Reminder date updates.
- Deadline handling.
- SQL validation.
- Parameterized execution.
- Confirmation requirements.
- Conversation memory.
- Natural-language date resolution.

All existing authorization and validation rules remain mandatory.
