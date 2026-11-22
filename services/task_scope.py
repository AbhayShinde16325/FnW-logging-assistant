"""Request-scoped employee task visibility.

The HTTP layer establishes one :class:`TaskActor` per authenticated request.
The repository and the service layer consult it so task reads and writes are
filtered by employee id, not only by a caller-supplied display name.

Assignment is enforced exclusively through ``assigned_to_employee_id``. The
RBAC migration backfills that id from an exact, unique display-name match
where possible; display names are never used as an authorization mechanism.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TaskActor:
    id: int
    name: str
    permissions: frozenset[str]

    @classmethod
    def from_employee(cls, employee) -> "TaskActor":
        return cls(
            id=int(employee.id),
            name=(employee.name or "").strip(),
            permissions=frozenset(employee.permissions),
        )

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_any_permission(self, permissions: Iterable[str]) -> bool:
        return bool(self.permissions & frozenset(permissions))


_CURRENT_ACTOR: ContextVar[TaskActor | None] = ContextVar(
    "task_flow_actor", default=None
)


@contextmanager
def use_task_actor(actor: TaskActor | None):
    token = _CURRENT_ACTOR.set(actor)

    try:
        yield actor
    finally:
        _CURRENT_ACTOR.reset(token)


def current_actor() -> TaskActor | None:
    return _CURRENT_ACTOR.get()


def _value(task, name):
    if isinstance(task, dict):
        return task.get(name)

    return getattr(task, name, None)


TASK_ASSIGNMENT_FIELDS = frozenset({"assigned_to", "assigned_to_employee_id"})


def owns_task(task, actor: TaskActor | None = None) -> bool:
    """True only when the task is assigned to the authenticated employee.

    Display names are never an authorization mechanism. Existing rows are
    linked to an employee id by the RBAC migration; a row without that link is
    visible to managers and administrators, not to ordinary employees.
    """

    actor = actor or current_actor()

    if actor is None:
        return True

    assigned_to = _value(task, "assigned_to_employee_id")

    try:
        return assigned_to is not None and int(assigned_to) == actor.id
    except (TypeError, ValueError):
        return False


def can_read_task(task, actor: TaskActor | None = None) -> bool:
    actor = actor or current_actor()

    if actor is None:
        return True

    if not actor.has_permission("tasks.read"):
        return False

    return actor.has_permission("tasks.read_all") or owns_task(task, actor)


def can_update_task(task, actor: TaskActor | None = None) -> bool:
    actor = actor or current_actor()

    if actor is None:
        return True

    if not actor.has_permission("tasks.update"):
        return False

    return actor.has_permission("tasks.update_all") or owns_task(task, actor)


def can_delete_task(task, actor: TaskActor | None = None) -> bool:
    actor = actor or current_actor()

    if actor is None:
        return True

    if not actor.has_permission("tasks.delete"):
        return False

    return actor.has_permission("tasks.update_all") or owns_task(task, actor)


def can_read_all(actor: TaskActor | None = None) -> bool:
    actor = actor or current_actor()
    return bool(actor and actor.has_permission("tasks.read_all"))


def can_update_all(actor: TaskActor | None = None) -> bool:
    actor = actor or current_actor()
    return bool(actor and actor.has_permission("tasks.update_all"))


def restricted_update_fields(
    updates,
    actor: TaskActor | None = None,
) -> set[str]:
    """Return requested fields an employee is not allowed to change.

    Employees may edit their own tasks but may never reassign them. Ownership
    is established by the actor and the task's employee foreign key, so
    accepting an assignee from the request would bypass that boundary.
    """

    actor = actor or current_actor()

    if actor is None or actor.has_permission("tasks.update_all"):
        return set()

    requested = set(updates or {})
    return requested & TASK_ASSIGNMENT_FIELDS


def query_scope(query, *, write: bool = False, actor: TaskActor | None = None):
    """Apply the employee assignment filter to an ORM ``Task`` query."""

    from database.models import Task

    actor = actor or current_actor()

    if actor is None:
        return query

    if write and actor.has_permission("tasks.update_all"):
        return query

    if not write and actor.has_permission("tasks.read_all"):
        return query

    return query.filter(Task.assigned_to_employee_id == actor.id)
