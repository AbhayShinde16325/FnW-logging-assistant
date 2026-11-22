"""Persistent, minimal conversation memory.

The memory of a conversation is one row of the ``conversation_memory`` table.
It is written through the ordinary SQLAlchemy session of the project, so it
survives a restart of the backend and needs no extra infrastructure.

What is stored is deliberately small:

* ``active_task_id`` - the task the employee is working on,
* ``last_action``    - the last action that was carried out successfully,
* ``context``        - a JSON structure with a snapshot of the active task and
  the last turns of the conversation (bounded, never the whole transcript).

The memory is looked up by ``user_id`` **and** ``conversation_id``, so two
users never see each other's context and a new conversation never inherits
the task of an older one.

Nothing in this module executes a task operation: it only reads and writes
its own table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from database.connection import SessionLocal
from database.models import ConversationMemory
from database.task_repository import get_task, get_task_for_owner
from services.task_scope import current_actor

logger = logging.getLogger(__name__)

# The number of messages that are kept in the context and sent to the model.
# The history is a short window, not a transcript of the whole conversation.
MAX_HISTORY_ENTRIES = 10

# The length of one stored turn. A long answer (a list of tasks) is cut off,
# so the context stays small.
MAX_TURN_LENGTH = 400

# A sentinel so an omitted feedback value never overwrites a stored one.
_UNSET = object()

# The fields of a task that are worth remembering.
TASK_SNAPSHOT_FIELDS = (
    "id",
    "task_title",
    "task_description",
    "assigned_to",
    "status",
    "progress",
    "deadline_date",
    "deadline_time",
)


# --- helpers --------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _actor_employee_id() -> int | None:
    actor = current_actor()
    return actor.id if actor is not None else None


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""

    return text or None


def _scoped_user_id(user_id) -> str | None:
    """Use the authenticated employee's id, never a caller-supplied value."""

    employee_id = _actor_employee_id()

    if employee_id is not None:
        return f"employee:{employee_id}"

    return _clean(user_id)


def _iso(value: datetime | None = None) -> str:
    """A sortable UTC timestamp for one stored message."""

    moment = value or _utcnow()

    return moment.isoformat(timespec="seconds")


def _clean_turn(turn: dict) -> dict:
    """One stored message: role, text and timestamp."""

    role = str((turn or {}).get("role") or "user").strip().lower()

    if role not in ("user", "assistant", "system"):
        role = "user"

    text = str((turn or {}).get("text") or "").strip()[:MAX_TURN_LENGTH]

    return {
        "role": role,
        "text": text,
        "at": str((turn or {}).get("at") or "").strip() or _iso(),
    }


def recent_messages(history, limit: int = MAX_HISTORY_ENTRIES) -> list[dict]:
    """The latest messages, arranged chronologically.

    The stored history is chronological. It is read in reverse order (newest
    first), the latest ``limit`` entries are kept, and the result is turned
    back into chronological order before it is shown to the model.
    """

    turns = [
        _clean_turn(turn)
        for turn in (history or [])
        if isinstance(turn, dict) and (turn.get("text") or turn.get("role"))
    ]

    if not turns:
        return []

    latest = list(reversed(turns))

    if limit is not None and limit >= 0:
        latest = latest[:limit]

    latest.reverse()

    return latest


def _field(task, name):
    if isinstance(task, dict):
        return task.get(name)

    return getattr(task, name, None)


def task_snapshot(task) -> dict:
    """The small snapshot of a task that is kept in the memory."""

    if task is None:
        return {}

    snapshot = {}

    for name in TASK_SNAPSHOT_FIELDS:
        value = _field(task, name)

        if hasattr(value, "isoformat"):
            value = value.isoformat()

        snapshot[name] = value

    return snapshot


def _snapshot(memory: ConversationMemory) -> dict:
    """A plain, detached view of one memory row."""

    return {
        "id": memory.id,
        "conversation_id": memory.conversation_id,
        "user_id": memory.user_id,
        "active_task_id": memory.active_task_id,
        "last_action": memory.last_action,
        "response_feedback": memory.response_feedback,
        "context": {
            **dict(memory.context or {}),
            "history": recent_messages(
                (memory.context or {}).get("history") or []
            ),
        },
        "updated_at": memory.updated_at,
    }


def _fetch(session, user_id: str, conversation_id: str):
    return (
        session.query(ConversationMemory)
        .filter(
            ConversationMemory.user_id == user_id,
            ConversationMemory.conversation_id == conversation_id,
        )
        .first()
    )


# --- the memory service ----------------------------------------------------------


def get_conversation_memory(user_id, conversation_id) -> dict | None:
    """Return the stored memory of one conversation.

    ``None`` is returned when the conversation has no memory yet, or when the
    user or the conversation is unknown. A missing memory is never an error:
    the conversation simply starts without a context.
    """

    user = _scoped_user_id(user_id)
    conversation = _clean(conversation_id)

    if not user or not conversation:
        return None

    session = SessionLocal()

    try:
        memory = _fetch(session, user, conversation)

        return _snapshot(memory) if memory is not None else None

    finally:
        session.close()


def save_conversation_memory(
    user_id,
    conversation_id,
    *,
    active_task_id: int | None = None,
    last_action: str | None = None,
    context: dict | None = None,
    clear_active_task: bool = False,
    response_feedback=_UNSET,
) -> dict | None:
    """Create the memory of a conversation, or update the stored one.

    The row is created when it does not exist yet, so there is never more
    than one memory per user and conversation.
    """

    user = _scoped_user_id(user_id)
    conversation = _clean(conversation_id)

    if not user or not conversation:
        return None

    session = SessionLocal()

    try:
        memory = _fetch(session, user, conversation)

        if memory is None:
            memory = ConversationMemory(
                user_id=user,
                conversation_id=conversation,
                employee_id=_actor_employee_id(),
                active_task_id=active_task_id,
                last_action=last_action,
                context=dict(context or {}),
                response_feedback=(
                    None
                    if response_feedback is _UNSET
                    else response_feedback
                ),
            )
            session.add(memory)
        else:
            if memory.employee_id is None:
                memory.employee_id = _actor_employee_id()

            if clear_active_task:
                memory.active_task_id = None
            elif active_task_id is not None:
                memory.active_task_id = active_task_id

            if last_action is not None:
                memory.last_action = last_action

            if context is not None:
                merged = dict(memory.context or {})
                merged.update(context)
                memory.context = merged

            if response_feedback is not _UNSET:
                memory.response_feedback = response_feedback

        memory.updated_at = _utcnow()

        session.commit()
        session.refresh(memory)

        return _snapshot(memory)

    except IntegrityError:
        # Another request created the row at the same time: update it.
        session.rollback()

        memory = _fetch(session, user, conversation)

        if memory is None:
            return None

        if clear_active_task:
            memory.active_task_id = None
        elif active_task_id is not None:
            memory.active_task_id = active_task_id

        if last_action is not None:
            memory.last_action = last_action

        if context is not None:
            merged = dict(memory.context or {})
            merged.update(context)
            memory.context = merged

        if response_feedback is not _UNSET:
            memory.response_feedback = response_feedback

        memory.updated_at = _utcnow()

        session.commit()
        session.refresh(memory)

        return _snapshot(memory)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def update_response_feedback(user_id, conversation_id, feedback):
    """Store a new rating for the conversation without touching its context.

    ``True`` is a thumbs up, ``False`` is a thumbs down. The row is looked up
    by ``user_id`` and ``conversation_id``, so a rating is always written to
    the memory of the conversation it belongs to.
    """

    if not isinstance(feedback, bool):
        return None

    return save_conversation_memory(
        user_id,
        conversation_id,
        response_feedback=feedback,
    )


def update_active_task(
    user_id,
    conversation_id,
    task_id: int | None,
    *,
    employee: str | None = None,
    task=None,
    last_action: str | None = None,
    context: dict | None = None,
) -> dict | None:
    """Remember the task the employee is working on.

    The task id is validated against the database first, and against the
    ownership of the employee when one is known: a task that does not exist,
    or a task of somebody else, is never stored. Passing ``task_id=None``
    clears the active task.
    """

    user = _scoped_user_id(user_id)
    conversation = _clean(conversation_id)

    if not user or not conversation:
        return None

    if task_id is None:
        return save_conversation_memory(
            user,
            conversation,
            context={"active_task": {}, **(context or {})},
            clear_active_task=True,
            last_action=last_action,
        )

    known = task
    actor = current_actor()

    if actor is not None:
        # Authenticated ownership is always re-read from the database.
        known = get_task_for_owner(task_id, actor.name)
    elif employee:
        known = get_task_for_owner(task_id, employee)
    elif known is None:
        known = get_task(task_id)

    if known is None:
        logger.info(
            "Task %s is not stored for %s, the active task is not changed.",
            task_id,
            user,
        )
        return None

    snapshot = task_snapshot(known)

    return save_conversation_memory(
        user,
        conversation,
        active_task_id=int(_field(known, "id") or task_id),
        context={"active_task": snapshot, **(context or {})},
        last_action=last_action,
    )


def get_recent_messages(
    user_id, conversation_id, limit: int = MAX_HISTORY_ENTRIES
) -> list[dict]:
    """Read the latest messages of one conversation from PostgreSQL.

    Only the row of this ``user_id`` **and** ``conversation_id`` is read, so
    histories of different users or conversations are never mixed. The
    messages are returned in chronological order.
    """

    memory = get_conversation_memory(user_id, conversation_id)

    if not memory:
        return []

    history = (memory.get("context") or {}).get("history") or []

    return recent_messages(history, limit)


def append_history(user_id, conversation_id, turns: list) -> dict | None:
    """Add the last turns of the conversation to the stored context.

    Only the last ``MAX_HISTORY_ENTRIES`` entries are kept, so the context
    never grows into a full transcript.
    """

    user = _scoped_user_id(user_id)
    conversation = _clean(conversation_id)

    if not user or not conversation:
        return None

    session = SessionLocal()

    try:
        memory = _fetch(session, user, conversation)

        if memory is None:
            memory = ConversationMemory(
                user_id=user,
                conversation_id=conversation,
                employee_id=_actor_employee_id(),
                context={},
            )
            session.add(memory)

        context = dict(memory.context or {})
        history = list(context.get("history") or [])
        history.extend(turns or [])
        context["history"] = recent_messages(history, MAX_HISTORY_ENTRIES)

        memory.context = context
        memory.updated_at = _utcnow()

        session.commit()
        session.refresh(memory)

        return _snapshot(memory)

    except IntegrityError:
        # The row was created by another request in the meantime.
        session.rollback()

        memory = _fetch(session, user, conversation)

        if memory is None:
            return None

        context = dict(memory.context or {})
        history = list(context.get("history") or [])
        history.extend(turns or [])
        context["history"] = recent_messages(history, MAX_HISTORY_ENTRIES)

        memory.context = context
        memory.updated_at = _utcnow()

        session.commit()
        session.refresh(memory)

        return _snapshot(memory)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def clear_conversation_memory(user_id, conversation_id) -> bool:
    """Forget everything about one conversation.

    ``True`` is returned when a memory was removed.
    """

    user = _scoped_user_id(user_id)
    conversation = _clean(conversation_id)

    if not user or not conversation:
        return False

    session = SessionLocal()

    try:
        memory = _fetch(session, user, conversation)

        if memory is None:
            return False

        session.delete(memory)
        session.commit()

        return True

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()
