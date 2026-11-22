"""Add Google authentication, sessions and database-backed RBAC.

Revision ID: 0001_auth_rbac
Revises:
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_auth_rbac"
down_revision: Union[str, None] = None
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
        "reports.read",
    ],
    "EMPLOYEE": [
        "tasks.create",
        "tasks.read",
        "tasks.update",
        "tasks.delete",
    ],
}


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_column(inspector, table: str, column: str) -> bool:
    if not inspector.has_table(table):
        return False

    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "roles"):
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False, unique=True),
            sa.Column("description", sa.String(), nullable=True),
        )
        op.create_index("ix_roles_id", "roles", ["id"])
        op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    if not _has_table(inspector, "permissions"):
        op.create_table(
            "permissions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False, unique=True),
            sa.Column("description", sa.String(), nullable=True),
        )
        op.create_index("ix_permissions_id", "permissions", ["id"])
        op.create_index(
            "ix_permissions_name", "permissions", ["name"], unique=True
        )

    inspector = sa.inspect(bind)

    if not _has_table(inspector, "role_permissions"):
        op.create_table(
            "role_permissions",
            sa.Column(
                "role_id",
                sa.Integer(),
                sa.ForeignKey("roles.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "permission_id",
                sa.Integer(),
                sa.ForeignKey("permissions.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )

    inspector = sa.inspect(bind)

    if not _has_table(inspector, "employees"):
        op.create_table(
            "employees",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("google_sub", sa.String(), nullable=True, unique=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("email", sa.String(), nullable=False, unique=True),
            sa.Column(
                "role_id",
                sa.Integer(),
                sa.ForeignKey("roles.id"),
                nullable=False,
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_employees_id", "employees", ["id"])
        op.create_index(
            "ix_employees_google_sub", "employees", ["google_sub"], unique=True
        )
        op.create_index(
            "ix_employees_email", "employees", ["email"], unique=True
        )
        op.create_index("ix_employees_role_id", "employees", ["role_id"])

    inspector = sa.inspect(bind)

    if not _has_table(inspector, "sessions"):
        op.create_table(
            "sessions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("refresh_token_hash", sa.String(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("user_agent", sa.String(), nullable=True),
            sa.Column("ip_address", sa.String(), nullable=True),
        )
        op.create_index("ix_sessions_employee_id", "sessions", ["employee_id"])
        op.create_index(
            "ix_sessions_refresh_token_hash",
            "sessions",
            ["refresh_token_hash"],
            unique=True,
        )
        op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    inspector = sa.inspect(bind)

    # A brand-new database has no application tables yet. In that case
    # create_tables() builds them from the current models after this
    # migration; there is nothing to alter here.
    if _has_table(inspector, "tasks"):
        if not _has_column(inspector, "tasks", "created_by_employee_id"):
            op.execute(
                "ALTER TABLE tasks ADD COLUMN created_by_employee_id INTEGER "
                "REFERENCES employees(id) ON DELETE SET NULL"
            )

        if not _has_column(inspector, "tasks", "assigned_to_employee_id"):
            op.execute(
                "ALTER TABLE tasks ADD COLUMN assigned_to_employee_id INTEGER "
                "REFERENCES employees(id) ON DELETE SET NULL"
            )

        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_tasks_created_by_employee_id "
            "ON tasks (created_by_employee_id)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_tasks_assigned_to_employee_id "
            "ON tasks (assigned_to_employee_id)"
        )

    if _has_table(inspector, "conversation_memory"):
        if not _has_column(inspector, "conversation_memory", "employee_id"):
            op.execute(
                "ALTER TABLE conversation_memory ADD COLUMN employee_id INTEGER "
                "REFERENCES employees(id) ON DELETE SET NULL"
            )

        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_conversation_memory_employee_id "
            "ON conversation_memory (employee_id)"
        )

    # Canonical role/permission catalogue. ON CONFLICT keeps this migration
    # safe for databases that were bootstrapped by create_all() first.
    permission_rows = {
        "tasks.create": "Create tasks.",
        "tasks.read": "Read tasks owned by the employee.",
        "tasks.read_all": "Read tasks across the organisation.",
        "tasks.update": "Update tasks owned by the employee.",
        "tasks.update_all": "Update any task.",
        "tasks.delete": "Delete tasks owned by the employee.",
        "employees.read": "View employees.",
        "employees.manage": "Create, update and deactivate employees.",
        "roles.manage": "Manage roles and permissions.",
        "reports.read": "Read organisation reports.",
    }
    role_rows = {
        "ADMIN": "Full access to tasks, employees, roles and reports.",
        "MANAGER": "Can read and manage all tasks plus team reports.",
        "EMPLOYEE": "Can create and manage their own assigned tasks.",
    }

    for name, description in permission_rows.items():
        op.execute(
            sa.text(
                "INSERT INTO permissions (name, description) "
                "VALUES (:name, :description) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description"
            ).bindparams(name=name, description=description)
        )

    for name, description in role_rows.items():
        op.execute(
            sa.text(
                "INSERT INTO roles (name, description) "
                "VALUES (:name, :description) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description"
            ).bindparams(name=name, description=description)
        )

    for role_name, permissions in ROLE_PERMISSIONS.items():
        for permission_name in permissions:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT roles.id, permissions.id FROM roles, permissions "
                    "WHERE roles.name = :role AND permissions.name = :permission "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role=role_name, permission=permission_name)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "conversation_memory"):
        op.execute("DROP INDEX IF EXISTS ix_conversation_memory_employee_id")
        op.execute(
            "ALTER TABLE conversation_memory DROP COLUMN IF EXISTS employee_id"
        )

    if _has_table(inspector, "tasks"):
        op.execute("DROP INDEX IF EXISTS ix_tasks_assigned_to_employee_id")
        op.execute("DROP INDEX IF EXISTS ix_tasks_created_by_employee_id")
        op.execute(
            "ALTER TABLE tasks DROP COLUMN IF EXISTS assigned_to_employee_id"
        )
        op.execute(
            "ALTER TABLE tasks DROP COLUMN IF EXISTS created_by_employee_id"
        )

    op.drop_table("sessions")
    op.drop_table("employees")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
