from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from database.connection import Base


def _utcnow() -> datetime:
    """The current UTC time, without a timezone (naive UTC)."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


class RolePermission(Base):
    """Association row between a role and one permission."""

    __tablename__ = "role_permissions"

    role_id = Column(
        Integer,
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    permission_id = Column(
        Integer,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False, unique=True, index=True)

    description = Column(String, nullable=True)

    roles = relationship(
        "Role",
        secondary="role_permissions",
        back_populates="permissions",
    )


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, nullable=False, unique=True, index=True)

    description = Column(String, nullable=True)

    permissions = relationship(
        "Permission",
        secondary="role_permissions",
        back_populates="roles",
        lazy="selectin",
    )

    employees = relationship("Employee", back_populates="role")


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)

    google_sub = Column(String, nullable=True, unique=True, index=True)

    name = Column(String, nullable=False)

    email = Column(String, nullable=False, unique=True, index=True)

    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)

    is_active = Column(Boolean, nullable=False, default=True)

    timezone = Column(
        String(64),
        nullable=False,
        default="Asia/Kolkata",
        server_default="Asia/Kolkata",
    )

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    updated_at = Column(
        DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    role = relationship("Role", back_populates="employees", lazy="joined")

    sessions = relationship(
        "AuthSession",
        back_populates="employee",
        cascade="all, delete-orphan",
    )


class AuthSession(Base):
    """A refresh-token session; only the hash of the token is stored."""

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    refresh_token_hash = Column(String, nullable=False, unique=True, index=True)

    expires_at = Column(DateTime, nullable=False, index=True)

    revoked_at = Column(DateTime, nullable=True)

    # The session created by rotating this token. A revoked token presented
    # again within the grace window while its replacement session is still active is a
    # benign duplicate (e.g. a concurrent browser refresh), not token reuse.
    replaced_by_session_id = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    last_used_at = Column(DateTime, nullable=True)

    user_agent = Column(String, nullable=True)

    ip_address = Column(String, nullable=True)

    employee = relationship("Employee", back_populates="sessions")


class MCPClientCredential(Base):
    """A long-lived, revocable MCP client credential.

    The plaintext credential is shown once at creation time. Only its SHA-256
    hash is stored. The credential authenticates MCP requests only and resolves
    to the associated active employee with the same RBAC/ownership rules.
    """

    __tablename__ = "mcp_client_credentials"

    id = Column(String(36), primary_key=True)

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name = Column(String(255), nullable=False)

    token_prefix = Column(String(32), nullable=False)

    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    created_by_employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    expires_at = Column(DateTime, nullable=True)

    revoked_at = Column(DateTime, nullable=True, index=True)

    last_used_at = Column(DateTime, nullable=True)

    employee = relationship(
        "Employee",
        foreign_keys=[employee_id],
    )


class MCPOAuthClient(Base):
    """Registered OAuth client for MCP (DCR, RFC 7591)."""

    __tablename__ = "mcp_oauth_clients"

    client_id = Column(String(255), primary_key=True)
    client_name = Column(String(255), nullable=True)
    redirect_uris = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    token_endpoint_auth_method = Column(String(64), nullable=False, default="none")
    grant_types = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    response_types = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    scope = Column(Text, nullable=True)
    client_uri = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)


class MCPOAuthCode(Base):
    """Short-lived OAuth authorization code for MCP ChatGPT clients.

    The plaintext code is never stored. Only a hash is persisted.
    """

    __tablename__ = "mcp_oauth_codes"

    id = Column(String(36), primary_key=True)
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id = Column(String(255), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    redirect_uri = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)
    code_challenge = Column(String(255), nullable=False)
    code_challenge_method = Column(String(16), nullable=False, default="S256")
    resource = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    employee = relationship("Employee")


class MCPOAuthSession(Base):
    """Refreshable MCP OAuth session for a ChatGPT/remote MCP client."""

    __tablename__ = "mcp_oauth_sessions"

    id = Column(String(36), primary_key=True)
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_id = Column(String(255), nullable=False, index=True)
    scope = Column(Text, nullable=True)
    resource = Column(Text, nullable=True)
    refresh_token_hash = Column(String(64), nullable=False, unique=True, index=True)
    access_jti = Column(String(64), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    replaced_by_session_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    last_used_at = Column(DateTime, nullable=True)
    user_agent = Column(String(512), nullable=True)
    ip_address = Column(String(64), nullable=True)

    employee = relationship("Employee")


class Task(Base):

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)

    task_title = Column(String, nullable=False)

    task_description = Column(String, nullable=True)

    assigned_to = Column(String, nullable=True)

    created_by_employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    assigned_to_employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    assigned_date = Column(Date, nullable=True)

    assigned_time = Column(Time, nullable=True)

    deadline_date = Column(Date, nullable=True)

    deadline_time = Column(Time, nullable=True)

    reminder_date = Column(Date, nullable=True)

    reminder_time = Column(Time, nullable=True)

    status = Column(String, default="PENDING")

    progress = Column(Integer, default=0)


class Notification(Base):
    """A persistent, employee-scoped notification/event.

    Rows are created by the reminder worker or task-event hooks and remain
    visible in the in-app notification center even when push delivery fails or
    the employee has no registered devices.
    """

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title = Column(String(255), nullable=False)

    message = Column(Text, nullable=False)

    notification_type = Column(
        String(64),
        nullable=False,
        default="TASK_REMINDER",
        server_default="TASK_REMINDER",
    )

    priority = Column(
        String(16),
        nullable=False,
        default="NORMAL",
        server_default="NORMAL",
    )

    voice_text = Column(Text, nullable=True)

    payload = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        default=dict,
    )

    is_read = Column(Boolean, nullable=False, default=False, server_default="false")

    scheduled_at = Column(DateTime, nullable=False, index=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    delivered_at = Column(DateTime, nullable=True)

    delivery_status = Column(
        String(32),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        index=True,
    )

    retry_count = Column(Integer, nullable=False, default=0, server_default="0")

    next_retry_at = Column(DateTime, nullable=True, index=True)

    last_error = Column(Text, nullable=True)

    locked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "task_id",
            "notification_type",
            "scheduled_at",
            name="uq_notifications_employee_task_type_scheduled",
        ),
    )


class PushSubscription(Base):
    """A browser/device Web Push subscription owned by one employee."""

    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    endpoint = Column(String(1024), nullable=False, unique=True, index=True)

    p256dh_key = Column(String(512), nullable=False)

    auth_key = Column(String(512), nullable=False)

    device_name = Column(String(255), nullable=True)

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(DateTime, nullable=False, default=_utcnow)

    updated_at = Column(
        DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    last_used_at = Column(DateTime, nullable=True)


class ConversationMemory(Base):
    """The small, persistent memory of one conversation.

    One row per ``(user_id, conversation_id)`` pair. It survives a restart of
    the backend and holds only what is needed to understand the next message:

    * ``active_task_id`` - the task the employee is working on,
    * ``last_action``    - the last action that was carried out,
    * ``context``        - a minimal JSON structure with a snapshot of the
      active task and the last turns of the conversation.

    The table is only used by the application (ORM access through
    ``services/conversation_memory.py``). It is never touched by the SQL
    generation skill, which stays limited to the ``tasks`` table.
    """

    __tablename__ = "conversation_memory"

    id = Column(Integer, primary_key=True, index=True)

    conversation_id = Column(String, nullable=False, index=True)

    user_id = Column(String, nullable=False, index=True)

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    active_task_id = Column(Integer, nullable=True, index=True)

    last_action = Column(String, nullable=True)

    context = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        default=dict,
    )

    response_feedback = Column(Boolean, nullable=True, default=None)

    updated_at = Column(
        DateTime,
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "conversation_id",
            name="uq_conversation_memory_user_conversation",
        ),
    )
