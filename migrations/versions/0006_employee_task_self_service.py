"""Allow employees to create and delete their own tasks.

Revision ID: 0006_employee_task_self_service
Revises: 0005_ensure_notification_metadata
Create Date: 2026-11-18

Ownership remains enforced by the application through
``assigned_to_employee_id``. This migration only grants the two additional
permissions; it deliberately does not grant ``tasks.read_all`` or
``tasks.update_all`` to employees.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_employee_task_self_service"
down_revision: Union[str, None] = "0005_ensure_notification_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMPLOYEE_PERMISSIONS = ("tasks.create", "tasks.delete")

PERMISSION_DESCRIPTIONS = {
    "tasks.create": "Create tasks; employees create tasks for themselves.",
    "tasks.update": "Update fields on assigned tasks.",
    "tasks.delete": "Delete assigned tasks; managers delete any task.",
}

ROLE_DESCRIPTIONS = {
    "EMPLOYEE": "Can create, manage and delete their own assigned tasks.",
}


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not (
        _has_table(inspector, "roles")
        and _has_table(inspector, "permissions")
        and _has_table(inspector, "role_permissions")
    ):
        return

    for name, description in PERMISSION_DESCRIPTIONS.items():
        op.execute(
            sa.text(
                "UPDATE permissions SET description = :description "
                "WHERE name = :name"
            ).bindparams(name=name, description=description)
        )

    for name, description in ROLE_DESCRIPTIONS.items():
        op.execute(
            sa.text(
                "UPDATE roles SET description = :description WHERE name = :name"
            ).bindparams(name=name, description=description)
        )

    for permission_name in EMPLOYEE_PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT roles.id, permissions.id FROM roles, permissions "
                "WHERE roles.name = 'EMPLOYEE' "
                "AND permissions.name = :permission "
                "ON CONFLICT DO NOTHING"
            ).bindparams(permission=permission_name)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not (
        _has_table(inspector, "roles")
        and _has_table(inspector, "permissions")
        and _has_table(inspector, "role_permissions")
    ):
        return

    for permission_name in EMPLOYEE_PERMISSIONS:
        op.execute(
            sa.text(
                "DELETE FROM role_permissions "
                "WHERE role_id = (SELECT id FROM roles WHERE name = 'EMPLOYEE') "
                "AND permission_id = "
                "    (SELECT id FROM permissions WHERE name = :permission)"
            ).bindparams(permission=permission_name)
        )

    op.execute(
        sa.text(
            "UPDATE roles SET description = :description WHERE name = 'EMPLOYEE'"
        ).bindparams(
            description="Can view assigned tasks and update their progress."
        )
    )
