"""Ensure intelligent notification metadata columns exist.

Revision ID: 0005_ensure_notification_metadata
Revises: 0004_intelligent_notification_fields
Create Date: 2026-11-17

Some deployments have the notifications table without Alembic version
tracking, so 0004 may never have been applied even though later code expects
its columns. This migration is additive and idempotent: it adds only the
missing ORM columns and preserves existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_ensure_notification_metadata"
down_revision: Union[str, None] = "0004_intelligent_notification_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_column(inspector, table: str, column: str) -> bool:
    if not inspector.has_table(table):
        return False
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "notifications"):
        return

    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    if not _has_column(inspector, "notifications", "priority"):
        op.add_column(
            "notifications",
            sa.Column(
                "priority",
                sa.String(length=16),
                nullable=False,
                server_default="NORMAL",
            ),
        )

    inspector = sa.inspect(bind)
    if not _has_column(inspector, "notifications", "voice_text"):
        op.add_column(
            "notifications",
            sa.Column("voice_text", sa.Text(), nullable=True),
        )

    inspector = sa.inspect(bind)
    if not _has_column(inspector, "notifications", "payload"):
        op.add_column(
            "notifications",
            sa.Column("payload", json_type, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "notifications"):
        return

    for column in ("payload", "voice_text", "priority"):
        inspector = sa.inspect(bind)
        if _has_column(inspector, "notifications", column):
            with op.batch_alter_table("notifications") as batch:
                batch.drop_column(column)
