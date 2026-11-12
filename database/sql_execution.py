"""Model-independent SQL execution layer for the FnW task database.

The module validates generated SQL against ``DATABASE_SCHEMA.md`` and enforces
explicit confirmation for destructive operations before anything is sent to the
database. It is intentionally free of any LLM/provider specifics: callers pass a
plain SQL string, optional parameters, and an optional confirmation phrase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_OPERATIONS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "DROP TABLE"})

CONFIRMATION_PHRASES = {
    "UPDATE": "CONFIRM UPDATE",
    "DELETE": "CONFIRM DELETE",
    "DROP TABLE": "CONFIRM DROP TABLE tasks",
}

_IMPACT_TEXT = {
    "SELECT": "Reads rows from tasks.",
    "INSERT": "Adds rows to tasks.",
    "UPDATE": "Updates the rows in tasks matched by WHERE.",
    "DELETE": "Permanently deletes the rows in tasks matched by WHERE.",
    "DROP TABLE": "Permanently removes the entire tasks table and all rows.",
}

_OPERATION_PATTERNS = {
    "SELECT": re.compile(r"^select\b", re.IGNORECASE),
    "INSERT": re.compile(r"^insert\s+into\s+tasks\b", re.IGNORECASE),
    "UPDATE": re.compile(r"^update\s+tasks\b", re.IGNORECASE),
    "DELETE": re.compile(r"^delete\s+from\s+tasks\b", re.IGNORECASE),
    "DROP TABLE": re.compile(
        r"^drop\s+table\s+(?:if\s+exists\s+)?tasks\s*$", re.IGNORECASE
    ),
}

_TABLE_REFERENCE_PATTERN = re.compile(
    r"\b(?:from|into|update|join)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)
_WHERE_PATTERN = re.compile(r"\bwhere\b", re.IGNORECASE)
_TAUTOLOGY_PATTERN = re.compile(r"\bwhere\s+(?:true|1\s*=\s*1)\b", re.IGNORECASE)
_SELECT_INTO_PATTERN = re.compile(r"\bselect\b.*\binto\b", re.IGNORECASE)
_SELECT_LOCK_PATTERN = re.compile(r"\bfor\s+(?:update|share)\b", re.IGNORECASE)
_JOIN_PATTERN = re.compile(r"\bjoin\b", re.IGNORECASE)
_PLACEHOLDER_PATTERN = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)|\$(\d+)")


@dataclass(frozen=True)
class SqlValidation:
    """Outcome of validating one SQL statement against the schema rules."""

    allowed: bool
    operation: str | None
    table: str | None
    requires_confirmation: bool
    uses_parameters: bool
    reason: str | None = None


def _strip_comments(sql: str) -> str:
    """Remove SQL comments while preserving string literals."""

    result: list[str] = []
    in_single_quote = False
    in_double_quote = False
    index = 0

    while index < len(sql):
        if not in_single_quote and not in_double_quote:
            if sql.startswith("--", index):
                while index < len(sql) and sql[index] != "\n":
                    index += 1
                continue

            if sql.startswith("/*", index):
                end = sql.find("*/", index + 2)
                index = len(sql) if end == -1 else end + 2
                continue

        char = sql[index]
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote

        result.append(char)
        index += 1

    return "".join(result)


def _split_statements(sql: str) -> list[str]:
    """Split raw SQL into top-level statements, respecting quoted strings."""

    statements: list[str] = []
    buffer: list[str] = []
    in_single_quote = False
    in_double_quote = False

    for char in sql:
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote

        if char == ";" and not in_single_quote and not in_double_quote:
            statements.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)

    return statements


def _mask_strings(sql: str) -> str:
    """Replace string literal contents with spaces so regex checks are safe."""

    chars: list[str] = []
    in_single_quote = False
    in_double_quote = False

    for char in sql:
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            chars.append(char)
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            chars.append(char)
        elif in_single_quote or in_double_quote:
            chars.append(" ")
        else:
            chars.append(char)

    return "".join(chars)


def _classify_operation(sql: str) -> str | None:
    for operation, pattern in _OPERATION_PATTERNS.items():
        if pattern.match(sql):
            return operation
    return None


def _table_references(masked_sql: str) -> list[str]:
    return [match.group(1).lower() for match in _TABLE_REFERENCE_PATTERN.finditer(masked_sql)]


def validate_sql(sql: str) -> SqlValidation:
    """Validate one SQL statement against the schema guardrails."""

    if not isinstance(sql, str) or not sql.strip():
        return SqlValidation(
            allowed=False,
            operation=None,
            table=None,
            requires_confirmation=False,
            uses_parameters=False,
            reason="SQL must be a non-empty string.",
        )

    stripped = _strip_comments(sql)
    statements = _split_statements(stripped)

    if len(statements) != 1:
        return SqlValidation(
            allowed=False,
            operation=None,
            table=None,
            requires_confirmation=False,
            uses_parameters=False,
            reason="Exactly one SQL statement is required.",
        )

    statement = statements[0]
    masked = _mask_strings(statement)
    operation = _classify_operation(masked)

    if operation is None:
        return SqlValidation(
            allowed=False,
            operation=None,
            table=None,
            requires_confirmation=False,
            uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
            reason="Unsupported or prohibited SQL operation.",
        )

    tables = _table_references(masked)
    if any(table != "tasks" for table in tables):
        return SqlValidation(
            allowed=False,
            operation=operation,
            table=None,
            requires_confirmation=False,
            uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
            reason="Only the tasks table may be referenced.",
        )

    if _JOIN_PATTERN.search(masked):
        return SqlValidation(
            allowed=False,
            operation=operation,
            table="tasks",
            requires_confirmation=False,
            uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
            reason="Joins are not allowed.",
        )

    if operation == "SELECT":
        if _SELECT_INTO_PATTERN.search(masked):
            return SqlValidation(
                allowed=False,
                operation=operation,
                table="tasks",
                requires_confirmation=False,
                uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
                reason="SELECT ... INTO is not allowed.",
            )

        if _SELECT_LOCK_PATTERN.search(masked):
            return SqlValidation(
                allowed=False,
                operation=operation,
                table="tasks",
                requires_confirmation=False,
                uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
                reason="SELECT ... FOR UPDATE/SHARE is not allowed.",
            )

    if operation in {"UPDATE", "DELETE"}:
        if not _WHERE_PATTERN.search(masked):
            return SqlValidation(
                allowed=False,
                operation=operation,
                table="tasks",
                requires_confirmation=False,
                uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
                reason=f"{operation} requires a WHERE clause.",
            )

        if _TAUTOLOGY_PATTERN.search(masked):
            return SqlValidation(
                allowed=False,
                operation=operation,
                table="tasks",
                requires_confirmation=False,
                uses_parameters=_PLACEHOLDER_PATTERN.search(masked) is not None,
                reason=f"{operation} requires a specific WHERE clause.",
            )

    uses_parameters = _PLACEHOLDER_PATTERN.search(masked) is not None

    return SqlValidation(
        allowed=True,
        operation=operation,
        table="tasks",
        requires_confirmation=operation in CONFIRMATION_PHRASES,
        uses_parameters=uses_parameters,
        reason=None,
    )


def confirmation_phrase(operation: str) -> str | None:
    """Return the exact confirmation phrase required for an operation."""

    return CONFIRMATION_PHRASES.get(operation)


def validate_confirmation(operation: str, confirmation: str | None) -> bool:
    """Return True when the confirmation exactly matches the required phrase."""

    expected = confirmation_phrase(operation)
    if expected is None:
        return True
    return (
        isinstance(confirmation, str)
        and confirmation.strip().upper() == expected.upper()
    )


def plan_sql(sql: str, params: object | None = None) -> dict:
    """Describe what would happen without executing anything."""

    validation = validate_sql(sql)
    if not validation.allowed:
        return {
            "status": "error",
            "message": validation.reason,
            "sql": sql,
            "params": params,
        }

    return {
        "status": "plan",
        "operation": validation.operation,
        "sql": sql,
        "params": params,
        "target_table": validation.table,
        "requires_confirmation": validation.requires_confirmation,
        "confirmation_phrase": (
            confirmation_phrase(validation.operation)
            if validation.requires_confirmation
            else None
        ),
        "impact": _IMPACT_TEXT[validation.operation],
    }


def _error_result(
    message: str,
    sql: str | None,
    params: object | None = None,
    operation: str | None = None,
) -> dict:
    return {
        "status": "error",
        "operation": operation,
        "message": message,
        "sql": sql,
        "params": params,
    }


def _confirmation_required_result(
    validation: SqlValidation,
    sql: str,
    params: object | None,
) -> dict:
    return {
        "status": "confirmation_required",
        "operation": validation.operation,
        "sql": sql,
        "params": params,
        "target_table": validation.table,
        "impact": _IMPACT_TEXT[validation.operation],
        "confirmation_phrase": confirmation_phrase(validation.operation),
        "message": "Explicit confirmation is required before executing this statement.",
    }


def execute_sql(
    sql: str,
    params: object | None = None,
    *,
    confirmation: str | None = None,
    operation: str | None = None,
    connection: object | None = None,
) -> dict:
    """Validate and optionally execute one SQL statement.

    ``connection`` must expose ``execute(sql, params)``. When a destructive
    operation is requested without a valid confirmation phrase, nothing is sent
    to the database and a ``confirmation_required`` result is returned instead.
    """

    validation = validate_sql(sql)
    if not validation.allowed:
        return _error_result(validation.reason, sql, params)

    if operation is not None:
        normalized = _normalize_operation_label(operation)
        if normalized != validation.operation:
            return _error_result(
                f"Operation '{operation}' does not match the SQL statement.",
                sql,
                params,
            )

    if validation.requires_confirmation and not validate_confirmation(
        validation.operation, confirmation
    ):
        return _confirmation_required_result(validation, sql, params)

    if connection is None:
        raise ValueError("A database connection is required to execute SQL.")

    connection.execute(sql, params if params is not None else {})

    return {
        "status": "success",
        "operation": validation.operation,
        "sql": sql,
        "params": params,
        "executed": True,
    }


def _normalize_operation_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().upper().replace("_", " ")


def run_sql_request(request: dict, *, connection: object | None = None) -> dict:
    """Execute one structured SQL request and return a structured result.

    Request shape::

        {
            "operation": "DELETE",
            "sql": "DELETE FROM tasks WHERE id = :id",
            "params": {"id": 7},
            "confirmation": "CONFIRM DELETE"
        }

    ``operation`` is optional but, when present, must match the SQL statement.
    """

    if not isinstance(request, dict):
        return _error_result("SQL request must be a JSON object.", None)

    sql = request.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return _error_result(
            "The 'sql' field is required and must be a non-empty string.", None
        )

    params = request.get("params")
    confirmation = request.get("confirmation")
    operation = request.get("operation")

    validation = validate_sql(sql)
    if not validation.allowed:
        return _error_result(validation.reason, sql, params)

    if operation is not None:
        normalized = _normalize_operation_label(operation)
        if normalized != validation.operation:
            return _error_result(
                f"Operation '{operation}' does not match the SQL statement.",
                sql,
                params,
            )

    if validation.requires_confirmation and not validate_confirmation(
        validation.operation, confirmation
    ):
        return _confirmation_required_result(validation, sql, params)

    return execute_sql(
        sql,
        params=params,
        confirmation=confirmation,
        connection=connection,
        operation=validation.operation,
    )
