import logging
import re

from sqlalchemy import or_, text

from database import search as task_search
from database.connection import SessionLocal
from database.models import Employee, Task
from database.sql_execution import SqlAlchemyConnection, execute_operation, execute_sql
from services import notification_service
from services.task_scope import current_actor, query_scope

logger = logging.getLogger(__name__)

REMINDER_IMPACTING_FIELDS = frozenset(
    {
        "reminder_date",
        "reminder_time",
        "deadline_date",
        "deadline_time",
        "assigned_to",
        "assigned_to_employee_id",
        "status",
    }
)


def _cancel_reminders_for_change(task_id, updates) -> None:
    if task_id is None:
        return

    updates = updates or {}
    completed = str(updates.get("status") or "").upper() in {
        "COMPLETED",
        "CANCELLED",
    }

    if not completed and not (set(updates) & REMINDER_IMPACTING_FIELDS):
        if updates.get("progress") != 100:
            return

    try:
        notification_service.cancel_pending_notifications(
            int(task_id),
            reason="The task changed before the reminder was delivered.",
        )
    except Exception:
        logger.exception(
            "Could not cancel pending reminders for task %s.", task_id
        )


def _operation_label(operation) -> str:
    """The operation label of a structured operation or a plain string."""

    if isinstance(operation, dict):
        value = operation.get("operation")
    elif isinstance(operation, str):
        value = operation
    else:
        return ""

    return str(value or "").strip().upper().replace("_", " ")


def _updated_columns(sql) -> list[str]:
    """The columns assigned by a validated ``UPDATE ... SET`` statement.

    Used only to decide whether pending reminders must be cancelled. The
    statement itself is still executed by the shared validation layer.
    """

    match = re.search(
        r"\bset\b(.*?)(?:\bwhere\b|\breturning\b|;|$)",
        sql or "",
        re.IGNORECASE | re.DOTALL,
    )

    if match is None:
        return []

    columns = []

    for assignment in match.group(1).split(","):
        name = assignment.split("=", 1)[0].strip().replace('"', "")
        name = name.rsplit(".", 1)[-1].strip().lower()

        if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            columns.append(name)

    return columns


def _write_side_effects(operation, sql, params) -> None:
    """Cancel stale reminders after a successful write.

    ``operation`` may be the structured operation produced by
    ``database.sql_generator`` or the operation label passed by the database
    agent. A plain label never carries the task id or the changed columns, so
    they are read from the bound parameters and the validated statement.
    """

    label = _operation_label(operation)
    params = params or {}

    if isinstance(operation, dict):
        task_id = operation.get("task_id")
        updates = operation.get("updates") or {}
    else:
        task_id = params.get("task_id", params.get("id"))
        updates = (
            {name: params.get(name) for name in _updated_columns(sql)}
            if label == "UPDATE"
            else {}
        )

    if label == "DELETE":
        _cancel_reminders_for_change(task_id, {"status": "CANCELLED"})
    elif label == "UPDATE":
        _cancel_reminders_for_change(task_id, updates)


def run_sql_operation(operation, *, confirmation=None):
    """Execute a validated structured operation inside one transaction.

    ``operation`` is the JSON shape produced by ``database.sql_generator``:
    the intent, the operation type, the table, the task id and the values.
    It is validated again by the execution layer before it reaches the
    database, so an invalid operation can never be executed.
    """

    db = SessionLocal()

    try:
        connection = SqlAlchemyConnection(db)

        result = execute_operation(
            operation,
            connection=connection,
            confirmation=confirmation,
        )

        if result.get("status") == "success":
            db.commit()

            if operation and operation.get("table") == "tasks":
                _write_side_effects(
                    operation,
                    operation.get("sql"),
                    operation.get("params"),
                )
        else:
            db.rollback()

        return result

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def execute_read_sql(
    sql,
    params=None,
    *,
    max_rows: int = 200,
    timeout_ms: int = 5000,
):
    """Execute one validated, read-only ``SELECT`` in a safe transaction.

    The caller has already validated the statement with
    ``database.sql_execution.validate_sql``. This function adds the runtime
    protections that are not visible in the SQL text:

    * the transaction is read-only,
    * a statement timeout applies,
    * at most ``max_rows`` rows are returned.

    Values are always bound; the statement is never rebuilt with the values.
    """

    db = SessionLocal()

    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))

        result = db.execute(text(sql), params or {})

        if not getattr(result, "returns_rows", False):
            raise ValueError("The read statement did not return rows.")

        rows = [dict(row) for row in result.mappings().all()]
        truncated = len(rows) > max_rows

        return {
            "status": "success",
            "operation": "SELECT",
            "rows": rows[:max_rows],
            "row_count": min(len(rows), max_rows),
            "truncated": truncated,
        }

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def execute_write_sql(
    sql,
    params=None,
    *,
    confirmation=None,
    operation=None,
):
    """Validate and execute one write statement through the shared layer.

    ``database.sql_execution.execute_sql`` applies the confirmation policy and
    the SQL guardrails. The session is committed only on success.
    """

    db = SessionLocal()

    try:
        connection = SqlAlchemyConnection(db)

        result = execute_sql(
            sql,
            params=params,
            confirmation=confirmation,
            operation=operation,
            connection=connection,
        )

        if result.get("status") == "success":
            db.commit()
            _write_side_effects(operation, sql, params)
        else:
            db.rollback()

        return result

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def create_task(task_details):

    db = SessionLocal()

    try:
        # Supports both Pydantic models and dictionaries
        if hasattr(task_details, "model_dump"):
            task_data = task_details.model_dump()
        else:
            task_data = dict(task_details)

        actor = current_actor()

        if actor is not None:
            task_data.setdefault("created_by_employee_id", actor.id)

            if not actor.has_permission("tasks.update_all"):
                # Defense in depth: even a repository caller cannot insert a
                # task for somebody else on behalf of an employee.
                task_data["assigned_to_employee_id"] = actor.id
                task_data["assigned_to"] = actor.name
                task_data["created_by_employee_id"] = actor.id

            elif not task_data.get("assigned_to_employee_id"):
                assigned_name = (task_data.get("assigned_to") or "").strip()

                if assigned_name:
                    assignees = db.query(Employee).filter(
                        Employee.name.ilike(assigned_name),
                        Employee.is_active.is_(True),
                    ).limit(2).all()

                    if len(assignees) == 1:
                        task_data["assigned_to_employee_id"] = assignees[0].id
                    elif assigned_name.lower() == actor.name.lower():
                        task_data["assigned_to_employee_id"] = actor.id

        # Ignore keys that have no matching column so the caller can pass
        # forward-compatible data without breaking the insert.
        task_data = {
            key: value
            for key, value in task_data.items()
            if hasattr(Task, key) and key != "id"
        }

        task = Task(**task_data)

        db.add(task)
        db.commit()
        db.refresh(task)

        return task

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def get_task(task_id):

    db = SessionLocal()

    try:
        query = db.query(Task).filter(Task.id == task_id)
        return query_scope(query).first()

    finally:
        db.close()


def get_all_tasks():

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task))
        return query.order_by(Task.id).all()

    finally:
        db.close()


def search_tasks(
    *,
    task_id: int | None = None,
    task_title: str | None = None,
    assigned_to: str | None = None,
    owner: str | None = None,
    query: str | None = None,
    status: str | None = None,
    limit: int | None = None,
):
    """Find tasks by id, title, assignee, status, or a free-text query.

    Natural-language text is split into meaningful concepts (see
    ``database.search``): "research about MCP" matches
    "Researching about MCP servers" because both reduce to the concepts
    ``research`` and ``mcp``. The database filters the candidates with
    case-insensitive ``ILIKE`` terms, then the candidates are ranked by
    relevance.

    ``owner`` is an exact, case-insensitive ownership scope. It is kept
    separate from ``assigned_to`` (which is a partial name filter) so a
    lookup can never read another employee's row.
    """

    db = SessionLocal()

    try:
        tasks = query_scope(db.query(Task))

        if task_id is not None:
            tasks = tasks.filter(Task.id == task_id)

        if status:
            tasks = tasks.filter(Task.status == status.strip().upper())

        if assigned_to:
            tasks = tasks.filter(
                Task.assigned_to.ilike(f"%{assigned_to.strip()}%")
            )

        if owner and current_actor() is None:
            tasks = tasks.filter(Task.assigned_to.ilike(owner.strip()))

        text = task_title or query

        if text:
            effective_status = status or task_search.status_from_text(text)

            if effective_status and not status:
                tasks = tasks.filter(
                    Task.status == effective_status.strip().upper()
                )

            search_text = (
                task_search.without_status(text)
                if effective_status
                else text
            )

            terms = task_search.sql_terms(search_text)

            if terms:
                patterns = [f"%{term}%" for term in terms]

                if task_title:
                    tasks = tasks.filter(
                        or_(*[Task.task_title.ilike(pattern) for pattern in patterns])
                    )
                else:
                    tasks = tasks.filter(
                        or_(
                            *[Task.task_title.ilike(pattern) for pattern in patterns],
                            *[Task.assigned_to.ilike(pattern) for pattern in patterns],
                            *[Task.status.ilike(pattern) for pattern in patterns],
                        )
                    )
            else:
                pattern = f"%{text.strip()}%"
                tasks = tasks.filter(Task.task_title.ilike(pattern))

        rows = tasks.order_by(Task.id.desc()).all()

        if text:
            search_text = (
                task_search.without_status(text)
                if (status or task_search.status_from_text(text))
                else text
            )

            fields = ("task_title",) if task_title else (
                "task_title",
                "assigned_to",
                "status",
            )

            rows = task_search.rank(rows, search_text, fields=fields)

        if limit is not None:
            rows = rows[:limit]

        return rows

    finally:
        db.close()


def get_task_field(task_id, field, *, owner=None):
    """Read the validated column(s) of one semantic task field.

    The field is checked against ``database.task_fields`` and the ORM model
    before anything is read, so an unknown column can never reach the database.
    The row is read fresh on every call; the result is never taken from the
    conversation memory.
    """

    from database import task_fields

    columns = task_fields.field_columns(field)

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task)).filter(Task.id == task_id)

        if owner and current_actor() is None:
            query = query.filter(Task.assigned_to.ilike(owner.strip()))

        task = query.first()

        if task is None:
            return None

        return {column: getattr(task, column) for column in columns}

    finally:
        db.close()


def get_task_for_owner(task_id, employee):
    """Return the task only when it belongs to the given employee."""

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task)).filter(Task.id == task_id)

        if employee and current_actor() is None:
            query = query.filter(Task.assigned_to.ilike(employee.strip()))

        return query.first()

    finally:
        db.close()


def find_identical_task(
    *,
    task_title,
    assigned_to=None,
    deadline_date=None,
):
    """Find a task that already describes the very same piece of work.

    Used to stop a repeated CREATE request from inserting a duplicate row.
    """

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task)).filter(
            Task.task_title.ilike(task_title.strip())
        )

        if assigned_to:
            query = query.filter(Task.assigned_to.ilike(assigned_to.strip()))
        else:
            query = query.filter(Task.assigned_to.is_(None))

        if deadline_date is not None:
            query = query.filter(Task.deadline_date == deadline_date)

        return query.order_by(Task.id.desc()).first()

    finally:
        db.close()


def count_tasks() -> int:
    """Return the number of stored tasks."""

    db = SessionLocal()

    try:
        return query_scope(db.query(Task)).count()

    finally:
        db.close()


def update_task(task_id, updates, *, owner=None):
    """Update the given fields of one task and return the stored record.

    When ``owner`` is given the row must belong to that employee, so an
    update can never touch a task owned by somebody else. Only the keys
    present in ``updates`` are written; every other value is preserved.
    """

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task), write=True).filter(Task.id == task_id)

        if owner and current_actor() is None:
            query = query.filter(Task.assigned_to.ilike(owner.strip()))

        task = query.first()

        if not task:
            return None

        if hasattr(updates, "model_dump"):
            updates = updates.model_dump(exclude_unset=True)

        for key, value in updates.items():

            if hasattr(Task, key) and key != "id":
                setattr(task, key, value)

        db.commit()
        db.refresh(task)

        _cancel_reminders_for_change(task_id, updates)

        return task

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def delete_task(task_id, *, owner=None):
    """Delete one task, optionally restricted to its owner."""

    db = SessionLocal()

    try:
        query = query_scope(db.query(Task), write=True).filter(Task.id == task_id)

        if owner and current_actor() is None:
            query = query.filter(Task.assigned_to.ilike(owner.strip()))

        task = query.first()

        if not task:
            return False

        db.delete(task)
        db.commit()

        _cancel_reminders_for_change(task_id, {"status": "CANCELLED"})

        return True

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()