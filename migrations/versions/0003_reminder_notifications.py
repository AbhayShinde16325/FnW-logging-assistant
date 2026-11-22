"""Add reminder notifications, push subscriptions and employee timezone.

Revision ID: 0003_reminder_notifications
Revises: 0002a_create_core_tables
Create Date: 2026-11-17

The migration is additive and guarded by inspector checks, so it is safe for
existing deployments. Task and employee rows are never deleted or recreated.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_reminder_notifications"
down_revision: Union[str, None] = "0002a_create_core_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_column(inspector, table: str, column: str) -> bool:
    if not inspector.has_table(table):
        return False

    return column in {item["name"] for item in inspector.get_columns(table)}


def _has_index(inspector, table: str, name: str) -> bool:
    if not inspector.has_table(table):
        return False

    return name in {item["name"] for item in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "employees") and not _has_column(
        inspector, "employees", "timezone"
    ):
        op.add_column(
            "employees",
            sa.Column(
                "timezone",
                sa.String(length=64),
                nullable=False,
                server_default="Asia/Kolkata",
            ),
        )

    inspector = sa.inspect(bind)

    if not _has_table(inspector, "notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column(
                "notification_type",
                sa.String(length=64),
                nullable=False,
                server_default="TASK_REMINDER",
            ),
            sa.Column(
                "is_read",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column(
                "delivery_status",
                sa.String(length=32),
                nullable=False,
                server_default="PENDING",
            ),
            sa.Column(
                "retry_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("locked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "employee_id",
                "task_id",
                "notification_type",
                "scheduled_at",
                name="uq_notifications_employee_task_type_scheduled",
            ),
        )

    inspector = sa.inspect(bind)

    for name, columns in (
        ("ix_notifications_id", ["id"]),
        ("ix_notifications_employee_id", ["employee_id"]),
        ("ix_notifications_task_id", ["task_id"]),
        ("ix_notifications_scheduled_at", ["scheduled_at"]),
        ("ix_notifications_delivery_status", ["delivery_status"]),
        ("ix_notifications_next_retry_at", ["next_retry_at"]),
    ):
        if _has_table(inspector, "notifications") and not _has_index(
            inspector, "notifications", name
        ):
            op.create_index(name, "notifications", columns)

    inspector = sa.inspect(bind)

    if not _has_table(inspector, "push_subscriptions"):
        op.create_table(
            "push_subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("endpoint", sa.String(length=1024), nullable=False),
            sa.Column("p256dh_key", sa.String(length=512), nullable=False),
            sa.Column("auth_key", sa.String(length=512), nullable=False),
            sa.Column("device_name", sa.String(length=255), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "endpoint", name="uq_push_subscriptions_endpoint"
            ),
        )

    inspector = sa.inspect(bind)

    for name, columns, unique in (
        ("ix_push_subscriptions_id", ["id"], False),
        ("ix_push_subscriptions_employee_id", ["employee_id"], False),
        ("ix_push_subscriptions_endpoint", ["endpoint"], True),
    ):
        if _has_table(inspector, "push_subscriptions") and not _has_index(
            inspector, "push_subscriptions", name
        ):
            op.create_index(
                name,
                "push_subscriptions",
                columns,
                unique=unique,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "push_subscriptions"):
        op.drop_table("push_subscriptions")

    inspector = sa.inspect(bind)

    if _has_table(inspector, "notifications"):
        op.drop_table("notifications")

    inspector = sa.inspect(bind)

    if _has_table(inspector, "employees") and _has_column(
        inspector, "employees", "timezone"
    ):
        with op.batch_alter_table("employees") as batch:
            batch.drop_column("timezone")
