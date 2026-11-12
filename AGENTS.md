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
