from sqlalchemy import or_

from database import search as task_search
from database.connection import SessionLocal
from database.models import Task
from database.sql_execution import SqlAlchemyConnection, execute_operation


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
        return db.query(Task).filter(
            Task.id == task_id
        ).first()

    finally:
        db.close()


def get_all_tasks():

    db = SessionLocal()

    try:
        return db.query(Task).order_by(Task.id).all()

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
        tasks = db.query(Task)

        if task_id is not None:
            tasks = tasks.filter(Task.id == task_id)

        if status:
            tasks = tasks.filter(Task.status == status.strip().upper())

        if assigned_to:
            tasks = tasks.filter(
                Task.assigned_to.ilike(f"%{assigned_to.strip()}%")
            )

        if owner:
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
        query = db.query(Task).filter(Task.id == task_id)

        if owner:
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
        query = db.query(Task).filter(Task.id == task_id)

        if employee:
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
        query = db.query(Task).filter(
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
        return db.query(Task).count()

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
        query = db.query(Task).filter(Task.id == task_id)

        if owner:
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
        query = db.query(Task).filter(Task.id == task_id)

        if owner:
            query = query.filter(Task.assigned_to.ilike(owner.strip()))

        task = query.first()

        if not task:
            return False

        db.delete(task)
        db.commit()

        return True

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()