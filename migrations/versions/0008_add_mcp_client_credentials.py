"""Add dedicated MCP client credentials.

Revision ID: 0008_add_mcp_client_credentials
Revises: 0007_refresh_session_replacement
Create Date: 2026-11-18

The migration is additive and guarded by inspector checks. It stores only a
SHA-256 hash of each credential; plaintext values are never persisted.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_add_mcp_client_credentials"
down_revision: Union[str, None] = "0007_refresh_session_replacement"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return inspector.has_table(name)


def _has_index(inspector, table: str, name: str) -> bool:
    if not inspector.has_table(table):
        return False

    return name in {item["name"] for item in inspector.get_indexes(table)}


def _has_unique_constraint(inspector, table: str, name: str) -> bool:
    if not inspector.has_table(table):
        return False

    return name in {
        item.get("name") for item in inspector.get_unique_constraints(table)
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "mcp_client_credentials"):
        op.create_table(
            "mcp_client_credentials",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("token_prefix", sa.String(length=32), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column(
                "created_by_employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "token_hash", name="uq_mcp_client_credentials_token_hash"
            ),
        )

    inspector = sa.inspect(bind)

    index_specs = [
        (
            "ix_mcp_client_credentials_employee_id",
            ["employee_id"],
        ),
        (
            "ix_mcp_client_credentials_created_by_employee_id",
            ["created_by_employee_id"],
        ),
        (
            "ix_mcp_client_credentials_revoked_at",
            ["revoked_at"],
        ),
    ]

    for index_name, columns in index_specs:
        if _has_table(inspector, "mcp_client_credentials") and not _has_index(
            inspector, "mcp_client_credentials", index_name
        ):
            op.create_index(
                index_name,
                "mcp_client_credentials",
                columns,
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "mcp_client_credentials"):
        return

    for index_name in (
        "ix_mcp_client_credentials_revoked_at",
        "ix_mcp_client_credentials_created_by_employee_id",
        "ix_mcp_client_credentials_employee_id",
    ):
        inspector = sa.inspect(bind)
        if _has_index(inspector, "mcp_client_credentials", index_name):
            op.drop_index(index_name, table_name="mcp_client_credentials")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "mcp_client_credentials"):
        op.drop_table("mcp_client_credentials")
