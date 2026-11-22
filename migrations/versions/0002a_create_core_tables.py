"""Create core task and conversation memory tables.

Revision ID: 0002a_create_core_tables
Revises: 0002_rbac_task_policy
Create Date: 2026-11-22

Fresh Supabase deployments have no pre-existing application tables. Migration
0001 creates auth/RBAC tables and only adds columns to tasks/conversation_memory
when those tables already exist. Migration 0003 creates notifications with a
foreign key to tasks.id. This migration creates the missing core tables in the
correct order while remaining idempotent for existing databases.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002a_create_core_tables"
down_revision: Union[str, None] = "0002_rbac_task_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_index(inspector, table: str, name: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return name in {item["name"] for item in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "tasks"):
        op.create_table(
            "tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_title", sa.String(), nullable=False),
            sa.Column("task_description", sa.String(), nullable=True),
            sa.Column("assigned_to", sa.String(), nullable=True),
            sa.Column(
                "created_by_employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "assigned_to_employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("assigned_date", sa.Date(), nullable=True),
            sa.Column("assigned_time", sa.Time(), nullable=True),
            sa.Column("deadline_date", sa.Date(), nullable=True),
            sa.Column("deadline_time", sa.Time(), nullable=True),
            sa.Column("reminder_date", sa.Date(), nullable=True),
            sa.Column("reminder_time", sa.Time(), nullable=True),
            sa.Column("status", sa.String(), nullable=True, server_default="PENDING"),
            sa.Column("progress", sa.Integer(), nullable=True, server_default="0"),
        )

    inspector = sa.inspect(bind)
    for name, columns in (
        ("ix_tasks_id", ["id"]),
        ("ix_tasks_created_by_employee_id", ["created_by_employee_id"]),
        ("ix_tasks_assigned_to_employee_id", ["assigned_to_employee_id"]),
    ):
        if _has_table(inspector, "tasks") and not _has_index(inspector, "tasks", name):
            op.create_index(name, "tasks", columns)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "conversation_memory"):
        op.create_table(
            "conversation_memory",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("active_task_id", sa.Integer(), nullable=True),
            sa.Column("last_action", sa.String(), nullable=True),
            sa.Column(
                "context",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            ),
            sa.Column("response_feedback", sa.Boolean(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "conversation_id",
                name="uq_conversation_memory_user_conversation",
            ),
        )

    inspector = sa.inspect(bind)
    for name, columns in (
        ("ix_conversation_memory_id", ["id"]),
        ("ix_conversation_memory_conversation_id", ["conversation_id"]),
        ("ix_conversation_memory_user_id", ["user_id"]),
        ("ix_conversation_memory_employee_id", ["employee_id"]),
        ("ix_conversation_memory_active_task_id", ["active_task_id"]),
    ):
        if _has_table(inspector, "conversation_memory") and not _has_index(
            inspector, "conversation_memory", name
        ):
            op.create_index(name, "conversation_memory", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Only drop tables that were introduced for fresh-schema initialization and
    # have no dependent tables managed by later migrations in normal flow.
    if _has_table(inspector, "conversation_memory"):
        op.drop_table("conversation_memory")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "tasks"):
        # Notifications depend on tasks; downgrade should follow migration order.
        op.drop_table("tasks")
