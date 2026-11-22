"""Tighten role mappings and backfill task/memory ownership.

Revision ID: 0002_rbac_task_policy
Revises: 0001_auth_rbac
Create Date: 2026-11-17

The backfills only link rows when an exact, case-insensitive display-name
match identifies one employee. Ambiguous or unmatched rows stay unassigned
and therefore remain visible only to managers and administrators.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_rbac_task_policy"
down_revision: Union[str, None] = "0001_auth_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROLE_PERMISSIONS = {
    "ADMIN": [
        "tasks.create",
        "tasks.read",
        "tasks.read_all",
        "tasks.update",
        "tasks.update_all",
        "tasks.delete",
        "employees.read",
        "employees.manage",
        "roles.manage",
        "reports.read",
    ],
    "MANAGER": [
        "tasks.create",
        "tasks.read",
        "tasks.read_all",
        "tasks.update",
        "tasks.update_all",
        "tasks.delete",
        "employees.read",
        "employees.manage",
        "reports.read",
    ],
    "EMPLOYEE": [
        "tasks.read",
        "tasks.update",
    ],
}

ROLE_DESCRIPTIONS = {
    "ADMIN": "Full access to tasks, employees, roles and reports.",
    "MANAGER": "Can manage all tasks, employees and team reports.",
    "EMPLOYEE": "Can view assigned tasks and update their progress.",
}

PERMISSION_DESCRIPTIONS = {
    "tasks.create": "Create and assign tasks.",
    "tasks.read": "Read tasks assigned to the employee.",
    "tasks.read_all": "Read tasks across the organisation.",
    "tasks.update": "Update progress on assigned tasks.",
    "tasks.update_all": "Update any task field.",
    "tasks.delete": "Delete tasks.",
    "employees.read": "View employees.",
    "employees.manage": "Create, update and deactivate employees.",
    "roles.manage": "Manage roles, permissions and administrator privileges.",
    "reports.read": "Read organisation reports.",
}


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _set_role_permissions(role_permissions: dict[str, list[str]]) -> None:
    for role_name in role_permissions:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions "
                "WHERE role_id = (SELECT id FROM roles WHERE name = :role)"
            ).bindparams(role=role_name)
        )

    for role_name, permissions in role_permissions.items():
        for permission_name in permissions:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT roles.id, permissions.id FROM roles, permissions "
                    "WHERE roles.name = :role AND permissions.name = :permission "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role=role_name, permission=permission_name)
            )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "permissions"):
        for name, description in PERMISSION_DESCRIPTIONS.items():
            op.execute(
                sa.text(
                    "UPDATE permissions SET description = :description "
                    "WHERE name = :name"
                ).bindparams(name=name, description=description)
            )

    if _has_table(inspector, "roles"):
        for name, description in ROLE_DESCRIPTIONS.items():
            op.execute(
                sa.text(
                    "UPDATE roles SET description = :description WHERE name = :name"
                ).bindparams(name=name, description=description)
            )

    if (
        _has_table(inspector, "roles")
        and _has_table(inspector, "permissions")
        and _has_table(inspector, "role_permissions")
    ):
        _set_role_permissions(ROLE_PERMISSIONS)

    inspector = sa.inspect(bind)

    if _has_table(inspector, "tasks") and _has_table(inspector, "employees"):
        op.execute(
            sa.text(
                "UPDATE tasks SET assigned_to_employee_id = ("
                "  SELECT employees.id FROM employees"
                "  WHERE LOWER(TRIM(employees.name)) = "
                "        LOWER(TRIM(tasks.assigned_to))"
                "  LIMIT 1"
                ") "
                "WHERE assigned_to_employee_id IS NULL "
                "AND assigned_to IS NOT NULL "
                "AND TRIM(assigned_to) <> '' "
                "AND ("
                "  SELECT COUNT(*) FROM employees"
                "  WHERE LOWER(TRIM(employees.name)) = "
                "        LOWER(TRIM(tasks.assigned_to))"
                ") = 1"
            )
        )

    if _has_table(inspector, "conversation_memory") and _has_table(
        inspector, "employees"
    ):
        op.execute(
            sa.text(
                "UPDATE conversation_memory SET employee_id = ("
                "  SELECT employees.id FROM employees"
                "  WHERE 'employee:' || employees.id = conversation_memory.user_id"
                "  LIMIT 1"
                ") "
                "WHERE employee_id IS NULL "
                "AND user_id LIKE 'employee:%'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE conversation_memory SET employee_id = ("
                "  SELECT employees.id FROM employees"
                "  WHERE LOWER(TRIM(employees.name)) = "
                "        LOWER(TRIM(conversation_memory.user_id))"
                "  LIMIT 1"
                ") "
                "WHERE employee_id IS NULL "
                "AND user_id IS NOT NULL "
                "AND TRIM(user_id) <> '' "
                "AND ("
                "  SELECT COUNT(*) FROM employees"
                "  WHERE LOWER(TRIM(employees.name)) = "
                "        LOWER(TRIM(conversation_memory.user_id))"
                ") = 1"
            )
        )


def downgrade() -> None:
    """Restore the previous role mapping.

    The ownership backfills are intentionally not reverted: they are additive
    links and removing them would discard reconciliation work.
    """

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    previous_permissions = {
        "ADMIN": [
            "tasks.create",
            "tasks.read",
            "tasks.read_all",
            "tasks.update",
            "tasks.update_all",
            "tasks.delete",
            "employees.read",
            "employees.manage",
            "roles.manage",
            "reports.read",
        ],
        "MANAGER": [
            "tasks.create",
            "tasks.read",
            "tasks.read_all",
            "tasks.update",
            "tasks.update_all",
            "tasks.delete",
            "employees.read",
            "reports.read",
        ],
        "EMPLOYEE": [
            "tasks.create",
            "tasks.read",
            "tasks.update",
            "tasks.delete",
        ],
    }

    if (
        _has_table(inspector, "roles")
        and _has_table(inspector, "permissions")
        and _has_table(inspector, "role_permissions")
    ):
        _set_role_permissions(previous_permissions)
