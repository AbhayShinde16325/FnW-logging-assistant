"""Track the session created by a refresh-token rotation.

This supports distinguishing a benign duplicate refresh (the rotated token is
presented again while its replacement session is still active, for example due
to concurrent browser requests) from genuine token reuse.

Revision ID: 0007_refresh_session_replacement
Revises: 0006_employee_task_self_service
Create Date: 2026-11-18

The migration is additive and guarded by inspector checks.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_refresh_session_replacement"
down_revision: Union[str, None] = "0006_employee_task_self_service"
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

    if _has_table(inspector, "sessions") and not _has_column(
        inspector, "sessions", "replaced_by_session_id"
    ):
        op.add_column(
            "sessions",
            sa.Column("replaced_by_session_id", sa.String(), nullable=True),
        )

    inspector = sa.inspect(bind)

    if _has_column(inspector, "sessions", "replaced_by_session_id") and not _has_index(
        inspector, "sessions", "ix_sessions_replaced_by_session_id"
    ):
        op.create_index(
            "ix_sessions_replaced_by_session_id",
            "sessions",
            ["replaced_by_session_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "sessions", "ix_sessions_replaced_by_session_id"):
        op.drop_index(
            "ix_sessions_replaced_by_session_id",
            table_name="sessions",
        )

    inspector = sa.inspect(bind)

    if _has_column(inspector, "sessions", "replaced_by_session_id"):
        op.drop_column("sessions", "replaced_by_session_id")
