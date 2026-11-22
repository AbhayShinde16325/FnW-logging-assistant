"""Add intelligent notification metadata fields.

Revision ID: 0004_intelligent_notification_fields
Revises: 0003_reminder_notifications
Create Date: 2026-11-17

Additive and guarded by inspector checks.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_intelligent_notification_fields"
down_revision: Union[str, None] = "0003_reminder_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_VERSION_NUM_LENGTH = 255
_ALEMBIC_VERSION_NUM_LEGACY_LENGTH = 32


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_column(inspector, table: str, column: str) -> bool:
    if not inspector.has_table(table):
        return False
    return column in {item["name"] for item in inspector.get_columns(table)}


def _alembic_version_num_needs_widening(inspector) -> bool:
    if not inspector.has_table("alembic_version"):
        return False

    for column in inspector.get_columns("alembic_version"):
        if column.get("name") != "version_num":
            continue

        column_type = column.get("type")
        length = getattr(column_type, "length", None)
        # length is None for unbounded VARCHAR/TEXT; widening is unnecessary.
        return length is not None and length < _ALEMBIC_VERSION_NUM_LENGTH

    return False


def _ensure_alembic_version_num_can_store_revision() -> None:
    """Widen alembic_version.version_num before Alembic writes this revision.

    Alembic updates alembic_version after upgrade() returns, so this must run
    during the migration. It is safe for both fresh and existing databases.
    """

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _alembic_version_num_needs_widening(inspector):
        return

    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=_ALEMBIC_VERSION_NUM_LEGACY_LENGTH),
        type_=sa.String(length=_ALEMBIC_VERSION_NUM_LENGTH),
        existing_nullable=False,
    )


def upgrade() -> None:
    _ensure_alembic_version_num_can_store_revision()

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

    # Only shrink if all stored revision identifiers fit in the legacy width.
    inspector = sa.inspect(bind)
    if not inspector.has_table("alembic_version"):
        return

    rows = bind.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
    if any(len(str(row[0])) > _ALEMBIC_VERSION_NUM_LEGACY_LENGTH for row in rows if row[0] is not None):
        return

    if _alembic_version_num_needs_widening(inspector):
        return

    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=_ALEMBIC_VERSION_NUM_LENGTH),
        type_=sa.String(length=_ALEMBIC_VERSION_NUM_LEGACY_LENGTH),
        existing_nullable=False,
    )
