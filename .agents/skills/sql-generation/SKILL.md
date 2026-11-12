---
name: SQL Generation (FnW tasks)
description: Generate and safely execute PostgreSQL for the FnW tasks table, with explicit confirmation before UPDATE, DELETE, or DROP TABLE.
---

# SQL Generation (FnW tasks)

This skill is provider-independent. It works with any LLM agent that can read
`DATABASE_SCHEMA.md` and call a JSON-shaped execution layer.

## 1. Read the schema first

Always read `DATABASE_SCHEMA.md` before generating SQL. If the file is missing,
stop and ask for the schema. Do not invent tables, columns, or value rules.

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

A confirmation is valid only when it matches the operation exactly. Bare
agreement such as `yes`, `ok`, `sure`, `go ahead`, or `do it` is not valid
confirmation.

## 7. Workflow

1. Read `DATABASE_SCHEMA.md`.
2. Classify the request.
3. If ambiguous, ask one clarifying question and stop.
4. If prohibited, refuse and explain why.
5. Generate parameterized SQL that references only `tasks`.
6. For `SELECT` and `INSERT`, execute without confirmation.
7. For `UPDATE`, `DELETE`, or `DROP TABLE tasks`, return a
   `confirmation_required` result with the exact SQL, parameters, target table,
   impact, and required confirmation phrase.
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

If the host does not expose that helper, follow the contract above and do not
execute destructive SQL without explicit confirmation.

## 9. Parameterization

Use placeholders supported by the executor, for example `:id` or `$1`. Never
inline untrusted values into SQL.

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

If the user says something like “delete the old ones” or “clear task 7’s
reminder” without enough detail, ask which field, row, or filter they mean.
Do not guess.
