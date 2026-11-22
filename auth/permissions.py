"""Canonical permission and role names used by the database-backed RBAC."""

from __future__ import annotations

TASKS_CREATE = "tasks.create"
TASKS_READ = "tasks.read"
TASKS_READ_ALL = "tasks.read_all"
TASKS_UPDATE = "tasks.update"
TASKS_UPDATE_ALL = "tasks.update_all"
TASKS_DELETE = "tasks.delete"
EMPLOYEES_READ = "employees.read"
EMPLOYEES_MANAGE = "employees.manage"
ROLES_MANAGE = "roles.manage"
REPORTS_READ = "reports.read"

ALL_PERMISSIONS = (
    TASKS_CREATE,
    TASKS_READ,
    TASKS_READ_ALL,
    TASKS_UPDATE,
    TASKS_UPDATE_ALL,
    TASKS_DELETE,
    EMPLOYEES_READ,
    EMPLOYEES_MANAGE,
    ROLES_MANAGE,
    REPORTS_READ,
)

ROLE_ADMIN = "ADMIN"
ROLE_MANAGER = "MANAGER"
ROLE_EMPLOYEE = "EMPLOYEE"

ROLE_PERMISSIONS = {
    ROLE_ADMIN: frozenset(ALL_PERMISSIONS),
    ROLE_MANAGER: frozenset(
        {
            TASKS_CREATE,
            TASKS_READ,
            TASKS_READ_ALL,
            TASKS_UPDATE,
            TASKS_UPDATE_ALL,
            TASKS_DELETE,
            EMPLOYEES_READ,
            EMPLOYEES_MANAGE,
            REPORTS_READ,
        }
    ),
    ROLE_EMPLOYEE: frozenset(
        {
            TASKS_CREATE,
            TASKS_READ,
            TASKS_UPDATE,
            TASKS_DELETE,
        }
    ),
}

ROLE_DESCRIPTIONS = {
    ROLE_ADMIN: "Full access to tasks, employees, roles and reports.",
    ROLE_MANAGER: "Can manage all tasks, employees and team reports.",
    ROLE_EMPLOYEE: "Can create, manage and delete their own assigned tasks.",
}

PERMISSION_DESCRIPTIONS = {
    TASKS_CREATE: "Create tasks; employees create tasks for themselves.",
    TASKS_READ: "Read tasks assigned to the employee.",
    TASKS_READ_ALL: "Read tasks across the organisation.",
    TASKS_UPDATE: "Update fields on assigned tasks.",
    TASKS_UPDATE_ALL: "Update any task field.",
    TASKS_DELETE: "Delete assigned tasks; managers delete any task.",
    EMPLOYEES_READ: "View employees.",
    EMPLOYEES_MANAGE: "Create, update and deactivate employees.",
    ROLES_MANAGE: "Manage roles, permissions and administrator privileges.",
    REPORTS_READ: "Read organisation reports.",
}

TASK_TURN_PERMISSIONS = frozenset(
    {TASKS_CREATE, TASKS_READ, TASKS_UPDATE, TASKS_DELETE}
)


def known_permission(name: str) -> bool:
    return name in ALL_PERMISSIONS


def role_names() -> tuple[str, ...]:
    return tuple(ROLE_PERMISSIONS)
