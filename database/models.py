from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from database.connection import Base


def _utcnow() -> datetime:
    """The current UTC time, without a timezone (naive UTC)."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


class Task(Base):

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)

    task_title = Column(String, nullable=False)

    assigned_to = Column(String, nullable=True)

    assigned_date = Column(Date, nullable=True)

    assigned_time = Column(Time, nullable=True)

    deadline_date = Column(Date, nullable=True)

    deadline_time = Column(Time, nullable=True)

    reminder_date = Column(Date, nullable=True)

    reminder_time = Column(Time, nullable=True)

    status = Column(String, default="PENDING")

    progress = Column(Integer, default=0)


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

    active_task_id = Column(Integer, nullable=True, index=True)

    last_action = Column(String, nullable=True)

    context = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        default=dict,
    )

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
