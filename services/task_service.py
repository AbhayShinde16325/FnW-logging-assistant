"""Business operations for the Task Flow Automation API.

The service layer is the only place that knows how an employee sentence
becomes a database operation. It is responsible for:

1. detecting the intent of the message (``ai.intent_detection``),
2. identifying the task the employee is talking about,
3. building a *structured* database operation (``database.sql_generator``),
4. validating that operation (``database.sql_generator.validate_operation``
   and ``database.sql_execution.validate_sql``),
5. executing it (``database.task_repository`` / ``database.sql_execution``),
6. serialising the stored row back to the caller.

``app.py`` only routes HTTP traffic here; the conversational state machine
lives in ``ai/query_session.py`` and the extraction in
``ai/text_extractor.py``.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from datetime_resolver import ResolutionStatus, resolve

from ai.intent_detection import (
    CLARIFICATION_REQUIRED,
    CREATE_TASK,
    DELETE_TASK,
    GENERAL_QUERY,
    GET_TASK,
    LIST_TASKS,
    UPDATE_TASK,
    detect_intent,
    is_explicit_create_request,
    is_explicit_update_request,
)
from ai.query_session import (
    FIELD_LABELS,
    FIELD_ORDER,
    STAGE_CONFIRM_FIELDS,
    STAGE_CONFIRM_REMINDER,
    STAGE_CONFIRM_DELETE,
    STAGE_CONFIRM_UPDATE,
    STAGE_FIELDS,
    STAGE_REMINDER,
    STAGE_SELECT_TASK,
    STAGE_UPDATE,
    TaskDraftEngine,
    TaskUpdateEngine,
)
from ai.text_extractor import TaskExtractor, sync_status_progress
from database import search as task_search
from database import task_fields
from database.sql_execution import confirmation_phrase
from database.sql_generator import (
    OperationError,
    build_delete_operation,
    build_select_operation,
    build_update_operation,
    validate_operation,
    validate_updates,
)
from database.task_repository import (
    create_task,
    get_all_tasks,
    get_task,
    run_sql_operation,
    search_tasks,
)
from services.conversation_memory import (
    append_history,
    get_conversation_memory,
    get_recent_messages,
    save_conversation_memory,
    update_active_task,
)
from services.query_rewriter import rewrite_query_with_status
from services.task_agent import ReadRequest, TaskAgent

logger = logging.getLogger(__name__)

extractor = TaskExtractor()

TIMEZONE = getattr(config, "TIMEZONE", "Asia/Kolkata")

TASK_PAYLOAD_FIELDS = (
    "id",
    "task_title",
    "assigned_to",
    "assigned_date",
    "assigned_time",
    "deadline_date",
    "deadline_time",
    "reminder_date",
    "reminder_time",
    "status",
    "progress",
)

GENERIC_TASK_REFERENCES = {
    "task",
    "my task",
    "this task",
    "that task",
    "the task",
    "existing task",
    "this particular task",
    "particular task",
    "it",
    "my tasks",
    "the tasks",
}

SELF_REFERENCES = {"my", "mine", "me", "myself"}

# How many candidate tasks are shown when a lookup is ambiguous. The whole
# ranked list stays in the draft, so an exact task id can still be chosen.
MAX_SELECTION_CANDIDATES = 10

# The conversational stages that belong to the "change an existing task" flow.
UPDATE_STAGES = frozenset(
    {STAGE_SELECT_TASK, STAGE_CONFIRM_UPDATE, STAGE_CONFIRM_DELETE}
)

# The conversational stages that belong to the "build a new task" flow.
CREATE_STAGES = frozenset(
    {
        STAGE_FIELDS,
        STAGE_CONFIRM_FIELDS,
        STAGE_REMINDER,
        STAGE_CONFIRM_REMINDER,
    }
)

# The stages that start a new turn. A message of such a turn can be a
# follow-up of the conversation, so it may be rewritten into a standalone
# instruction. Every other stage belongs to a running conversation.
FRESH_STAGES = frozenset({None, "", "start", STAGE_UPDATE})

# A message that can pick one of the displayed task candidates.
SELECTION_SIGNAL_PATTERN = re.compile(
    r"(?:#|task\s*#?\s*|id\s*#?\s*)\d+"
    r"|\b(?:first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|"
    r"last|latest|previous|prior)\b"
    r"|\b(?:option|number|no\.?)\s*\d+\b"
    r"|\b\d+\b"
    r"|\b(?:that|this|the previous|the last) one\b",
    re.IGNORECASE,
)

# Words that turn a message into a new operation instead of a selection.
NEW_OPERATION_PATTERN = re.compile(
    r"\b(update|change|set|make|mark|move|rename|complete|finish|start|"
    r"begin|assign|schedule|remind|deadline|progress|percent|status|"
    r"assignee|title|date|time)\b|%",
    re.IGNORECASE,
)

UPDATE_ENGINE = TaskUpdateEngine()


# --- helpers --------------------------------------------------------------------


def _task_payload(task) -> dict:
    if isinstance(task, dict):
        return task

    return {
        name: getattr(task, name, None)
        for name in TASK_PAYLOAD_FIELDS
    }


def _field(task, name, default=None):
    """Read one field from an ORM task or from a database row."""

    if isinstance(task, dict):
        return task.get(name, default)

    return getattr(task, name, default)


def serialize_task(task) -> dict:
    """Serialise a task (ORM object or database row) for the API."""

    payload = _task_payload(task)

    serialized = dict(payload)

    for name in ("assigned_date", "deadline_date", "reminder_date"):
        value = serialized.get(name)
        if isinstance(value, date):
            serialized[name] = value.isoformat()

    for name in ("assigned_time", "deadline_time", "reminder_time"):
        value = serialized.get(name)
        if value is not None and hasattr(value, "strftime"):
            serialized[name] = value.strftime("%H:%M")

    if serialized.get("progress") is not None:
        serialized["progress"] = int(serialized["progress"])

    return serialized


def _task_choice_line(task) -> str:
    payload = _task_payload(task)
    return (
        f"#{payload.get('id')} — {payload.get('task_title') or 'Untitled task'} "
        f"({payload.get('assigned_to') or 'unassigned'}, "
        f"{payload.get('status') or 'PENDING'}, "
        f"{payload.get('progress') or 0}%)"
    )


def _task_selection_response(
    message: str,
    tasks: list,
    *,
    action: str = "update",
    intent: str = CLARIFICATION_REQUIRED,
    stage: str = STAGE_SELECT_TASK,
    changes: dict | None = None,
) -> dict:
    payloads = [serialize_task(task) for task in tasks if task is not None]

    # The candidates are already ranked by relevance. Only the best ones are
    # shown, but the whole list stays in the draft so an exact task id can
    # still be picked.
    visible = payloads[:MAX_SELECTION_CANDIDATES]

    reply = message
    if visible:
        lines = "\n".join(_task_choice_line(task) for task in visible)
        reply = (
            f"{message}\n\n{lines}\n\n"
            f"Tell me the task ID or repeat the {action} with the exact title."
        )

        if len(payloads) > len(visible):
            reply = (
                f"{reply}\n\nAnd {len(payloads) - len(visible)} more match(es); "
                "give me the exact task ID to pick one."
            )
    else:
        reply = f"{message}\n\nNo tasks were found."

    response = {
        "status": "task_selection_required",
        "intent": intent,
        "stage": stage,
        "message": reply,
        "reply": reply,
        "tasks": payloads,
        "draft": {
            "intent": intent,
            "action": action,
            "candidates": payloads,
            "changes": dict(changes or {}),
        },
    }

    return response


def _clean_reference(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or text.lower() in GENERIC_TASK_REFERENCES:
        return None
    if text.lower() in SELF_REFERENCES:
        return None
    return text


def _owner_for(find, employee: str | None) -> str | None:
    """The employee the request is scoped to, when one is known."""

    reference = (getattr(find, "assigned_to", None) or "").strip()

    if reference.lower() in SELF_REFERENCES:
        return (employee or "").strip() or None

    if employee:
        return employee.strip()

    return reference or None


def _resolve_update_phrase(phrase: str):
    result = resolve(phrase, now=datetime.now(ZoneInfo(TIMEZONE)),
                     timezone=TIMEZONE)

    question = None
    if result.status == ResolutionStatus.NEEDS_CLARIFICATION:
        if result.clarification:
            question = result.clarification.question
        else:
            question = "Please give a more specific date or time."
    elif result.status == ResolutionStatus.INVALID:
        question = result.error or "I couldn't understand that date or time."

    return result.resolved_date, result.resolved_time, question


def _build_update_changes(changes):
    """Turn the extracted changes into a validated {column: value} mapping."""

    updates: dict = {}

    for kind in ("assigned", "deadline", "reminder"):
        phrase = getattr(changes, f"{kind}_when", None)
        if not phrase:
            continue

        date_value, time_value, question = _resolve_update_phrase(phrase)

        if question and date_value is None and time_value is None:
            return None, question

        if date_value is not None:
            updates[f"{kind}_date"] = date_value
        if time_value is not None:
            updates[f"{kind}_time"] = time_value

    for name in ("task_title", "assigned_to"):
        value = getattr(changes, name, None)
        if value and value.strip():
            updates[name] = value.strip()

    if changes.status:
        updates["status"] = changes.status

    if changes.progress is not None:
        updates["progress"] = changes.progress

    return updates, None


def _sync_update_status_progress(task, updates: dict) -> dict:
    """Keep status and progress consistent with the schema value rules."""

    status, progress = sync_status_progress(
        updates.get("status", _field(task, "status")),
        updates.get("progress", _field(task, "progress")),
        status_changed="status" in updates,
        progress_changed="progress" in updates,
    )

    if status != _field(task, "status"):
        updates["status"] = status
    if progress != _field(task, "progress"):
        updates["progress"] = progress

    return updates


def _update_confirmation(
    user_input: str, changes: dict, *, staged: bool = False
) -> str | None:
    """The confirmation phrase used for an UPDATE statement.

    The SQL skill requires the exact phrase before an UPDATE runs. In the
    application the phrase is only granted when the employee explicitly asked
    for this change and the change set is not empty, so the gate can never be
    satisfied by accident:

    * ``staged`` - the employee already asked for the change in an earlier
      turn of the same conversation and is only picking the task now,
    * otherwise - the current message is itself an explicit update request.
    """

    if not changes:
        return None

    if staged:
        return confirmation_phrase("UPDATE")

    if detect_intent(user_input).name != UPDATE_TASK:
        return None

    return confirmation_phrase("UPDATE")


def _describe_changes(updates: dict) -> str:
    return "; ".join(
        f"{FIELD_LABELS.get(name, name)}: {_display_value(name, value)}"
        for name, value in updates.items()
        if name in FIELD_LABELS
    )


# --- task identification ---------------------------------------------------------


def _search_employee_tasks(
    *,
    title: str | None = None,
    assignee: str | None = None,
    owner: str | None = None,
    status: str | None = None,
) -> list:
    """Search tasks, always scoped to the employee when one is known.

    The title search understands natural language (see
    ``database.task_repository.search_tasks``): keywords, inflections,
    synonyms and spelling variants all reach the same task. ``owner`` is an
    exact ownership scope, so a search can never return another employee's
    task.
    """

    matches = search_tasks(
        task_title=title,
        assigned_to=assignee,
        owner=owner,
        status=status,
    )

    if not matches and title and assignee:
        matches = search_tasks(
            task_title=title, owner=owner, status=status
        )

    if not matches and title and assignee:
        matches = search_tasks(
            assigned_to=assignee, owner=owner, status=status
        )

    return matches


def _find_update_task(find, task_id: int | None, owner: str | None) -> list:
    """Find the task an update request is talking about."""

    if task_id is None:
        task_id = getattr(find, "task_id", None)

    if task_id is not None:
        task = get_task(task_id)
        if task is None:
            return []

        if owner and (_field(task, "assigned_to") or "").lower() != owner.strip().lower():
            return []

        return [task]

    title = _clean_reference(getattr(find, "task_title", None))
    assignee = _clean_reference(getattr(find, "assigned_to", None))

    if title or assignee:
        return _search_employee_tasks(
            title=title, assignee=assignee, owner=owner
        )

    if owner:
        return _search_employee_tasks(owner=owner)

    return []


def _find_query_tasks(query, task_id: int | None, owner: str | None) -> list:
    """Find the task(s) a read request is talking about."""

    find = query.find
    filters = query.filters

    if task_id is None:
        task_id = getattr(find, "task_id", None)

    if task_id is not None:
        task = get_task(task_id)
        if task is None:
            return []

        if owner and (_field(task, "assigned_to") or "").lower() != owner.strip().lower():
            return []

        return [task]

    title = _clean_reference(
        getattr(find, "task_title", None)
        or getattr(filters, "title_contains", None)
    )
    assignee = _clean_reference(
        getattr(find, "assigned_to", None)
        or getattr(filters, "assigned_to", None)
    )
    status = getattr(filters, "status", None)

    if title or assignee:
        return _search_employee_tasks(
            title=title, assignee=assignee, owner=owner, status=status
        )

    if status:
        return _search_employee_tasks(owner=owner, status=status)

    if owner:
        return _search_employee_tasks(owner=owner)

    return get_all_tasks()


# --- update flow -----------------------------------------------------------------


def process_update(
    user_input: str,
    task_id: int | None = None,
    *,
    employee: str | None = None,
    stage: str | None = None,
    draft: dict | None = None,
    confirm: bool = False,
    context_task_id: int | None = None,
) -> dict:
    """Update an existing task: find it, change only what was asked, save it."""

    draft = draft or {}

    # --- continue a running update conversation ---------------------------
    if stage == STAGE_SELECT_TASK:
        selection = UPDATE_ENGINE.resolve_selection(draft, user_input)

        if selection is None:
            return _task_selection_response(
                "I still need to know which task to update.",
                draft.get("candidates") or [],
                changes=draft.get("changes") or {},
            )

        return _apply_update(
            selection["task_id"],
            selection.get("changes") or draft.get("changes") or {},
            employee=employee,
            user_input=user_input,
            staged=True,
        )

    if stage == STAGE_CONFIRM_UPDATE:
        pending = dict(draft.get("changes") or {})
        pending_id = draft.get("task_id")
        new_request = None

        if not confirm and not _is_affirmation(user_input):
            # The employee is still telling us what to change: read the new
            # values and merge them into the pending change set.
            new_request = _extract_update_request(user_input)

            if new_request is not None:
                changes, question = _build_update_changes(new_request.changes)

                if question and not changes:
                    return _pending_update_response(
                        draft, pending_id, question
                    )

                pending.update(changes or {})

        if not pending:
            reply = (
                "Tell me the field and the new value, for example "
                "'progress is 80 percent'."
            )
            return _pending_update_response(draft, pending_id, reply)

        if pending_id is None:
            # The employee has not named the task yet. Work it out from the
            # criteria of the whole conversation.
            target, selection = _resolve_pending_target(
                draft, new_request, task_id=task_id, employee=employee,
                user_input=user_input,
            )

            if selection is not None:
                return selection

            pending_id = target

        if pending_id is None:
            reply = (
                "I still need to know which task to update. Tell me the task "
                "ID or repeat the title."
            )
            return _pending_update_response(draft, pending_id, reply)

        return _apply_update(
            pending_id, pending, employee=employee, user_input=user_input,
            staged=True,
        )

    # --- a brand new update request --------------------------------------
    try:
        update_request = extractor.extract_update(user_input)
    except RuntimeError as error:
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": str(error),
            "reply": str(error),
        }

    update_request = _trusted_task_id(user_input, update_request)

    find = update_request.find

    context_target = _context_target_for(
        user_input, update_request.find, context_task_id
    )

    if context_target is not None:
        if task_id is None:
            task_id = context_target

        if getattr(update_request.find, "task_id", None) is not None and not _mentioned_task_id(
            user_input, update_request.find.task_id
        ):
            update_request.find.task_id = None

    updates, question = _build_update_changes(update_request.changes)

    if question and not updates:
        return {
            "status": "clarification_required",
            "intent": CLARIFICATION_REQUIRED,
            "stage": STAGE_SELECT_TASK,
            "message": question,
            "reply": question,
        }

    if not updates:
        reply = (
            "What should I change? Tell me the field and the new value, for "
            "example 'change the deadline to 16 November 2026'."
        )
        owner = _owner_for(update_request.find, employee)
        candidates = _find_update_task(
            update_request.find, task_id, owner
        )

        return {
            "status": "clarification_required",
            "intent": CLARIFICATION_REQUIRED,
            "stage": STAGE_CONFIRM_UPDATE,
            "draft": UPDATE_ENGINE.pending_draft(
                update_request.find.task_id,
                updates,
                find=_find_payload(update_request.find),
                candidates=[serialize_task(task) for task in candidates],
            ),
            "task_id": update_request.find.task_id,
            "message": reply,
            "reply": reply,
        }

    owner = _owner_for(update_request.find, employee)

    matches = _find_update_task(update_request.find, task_id, owner)

    if not matches:
        all_tasks = get_all_tasks()

        has_criteria = any(
            [
                update_request.find.task_id is not None,
                _clean_reference(update_request.find.task_title),
                _clean_reference(update_request.find.assigned_to),
                task_id is not None,
            ]
        )

        if not has_criteria and len(all_tasks) == 1:
            matches = all_tasks
        else:
            message = (
                "I couldn't find a matching task, so nothing was changed."
                if has_criteria
                else "Which task should I update?"
            )
            return _task_selection_response(
                message, all_tasks, changes=updates
            )

    if len(matches) > 1:
        return _task_selection_response(
            "I found more than one matching task. Which one should I update?",
            matches,
            changes=updates,
        )

    return _apply_update(
        _field(matches[0], "id"), updates, employee=employee,
        user_input=user_input
    )


def _merge_pending_changes(pending: dict, user_input: str):
    """Read the values of a still running update conversation.

    Returns ``(changes, question)``. The new values are merged into the
    pending change set, so nothing that was already agreed is lost.
    """

    merged = dict(pending or {})

    request = _extract_update_request(user_input)

    if request is None:
        return merged, None

    changes, question = _build_update_changes(request.changes)

    if question and not changes:
        return merged, question

    merged.update(changes or {})

    return merged, None


# A task id the employee actually wrote: "#211", "task 211", "task #211".
MENTIONED_TASK_ID_PATTERN = re.compile(
    r"(?:#|task\s*#?\s*|id\s*#?\s*)(\d+)", re.IGNORECASE
)


def _trusted_task_id(user_input: str, request):
    """Drop a task id the extraction model invented.

    The model may only return a task id that the employee (or the rewritten
    query) actually stated. A title or another number in the sentence must
    never turn into an id, so an id that is not mentioned is removed and the
    search falls back to the title.
    """

    find = getattr(request, "find", None)

    if find is None or getattr(find, "task_id", None) is None:
        return request

    # When the request also carries a title or an assignee, the search can
    # fall back to it. An id that the employee did not write is then dropped.
    # A request that only names an id is left alone (the direct id lookup is
    # still validated against the database and the owner).
    has_fallback = bool(
        getattr(find, "task_title", None)
        or getattr(find, "assigned_to", None)
    )

    if not has_fallback:
        return request

    mentioned = MENTIONED_TASK_ID_PATTERN.search(user_input or "")

    if mentioned is None or int(mentioned.group(1)) != find.task_id:
        find.task_id = None

    return request


def _extract_update_request(user_input: str):
    """Ask the extractor for an update request, or None when it fails."""

    try:
        return _trusted_task_id(user_input, extractor.extract_update(user_input))
    except RuntimeError:
        return None


def _extract_query_request(user_input: str):
    """Ask the extractor for a read request, or None when it fails."""

    try:
        return _trusted_task_id(user_input, extractor.extract_query(user_input))
    except RuntimeError:
        return None


REFERENCE_TO_ACTIVE_TASK_PATTERN = re.compile(
    r"\b(it|its|this|that|same|the task|that task|this task|"
    r"the same task|the same one)\b",
    re.IGNORECASE,
)


def _has_task_criteria(find, filters=None) -> bool:
    """Does the request itself identify a task, without the conversation?"""

    criteria = [
        _clean_reference(getattr(find, "task_title", None)),
        _clean_reference(getattr(find, "assigned_to", None)),
    ]

    if filters is not None:
        criteria.extend(
            [
                _clean_reference(getattr(filters, "title_contains", None)),
                _clean_reference(getattr(filters, "assigned_to", None)),
                getattr(filters, "status", None),
            ]
        )

    return any(criteria)


def _mentioned_task_id(user_input: str, task_id) -> bool:
    """Did the employee actually write this task id?"""

    if task_id is None:
        return False

    for match in MENTIONED_TASK_ID_PATTERN.finditer(user_input or ""):
        if int(match.group(1)) == task_id:
            return True

    return False


def _context_target_for(
    user_input: str,
    find,
    context_task_id: int | None,
    *,
    filters=None,
    requested_field: str | None = None,
) -> int | None:
    """The active task to use only when the message points back to it.

    An explicit id, a title or an assignee always wins. The conversation's
    active task is only a fallback for a follow-up such as "what is its
    deadline?"; it never replaces a task the employee named.
    """

    if context_task_id is None:
        return None

    if _has_task_criteria(find, filters):
        return None

    task_id = getattr(find, "task_id", None)

    if task_id is not None:
        if _mentioned_task_id(user_input, task_id):
            return None

        # The extraction model returned an id the employee did not write.
        return context_task_id

    if requested_field is not None:
        return context_task_id

    if REFERENCE_TO_ACTIVE_TASK_PATTERN.search(user_input or ""):
        return context_task_id

    return None


def _active_task_id(memory: dict | None) -> int | None:
    """The task the conversation is currently about."""

    if not memory:
        return None

    task_id = (memory or {}).get("active_task_id")

    if task_id is not None:
        return task_id

    snapshot = ((memory or {}).get("context") or {}).get("active_task") or {}

    return snapshot.get("id")


def _read_task_field(task_id: int, field: str | None, *, employee=None) -> dict:
    """Read one validated field through the service's task lookup."""

    normalized = task_fields.normalize_field(field)

    if normalized is None or normalized == task_fields.FULL_DETAILS:
        return {
            "status": "error",
            "message": "The requested field is not available in the task database.",
        }

    task = get_task(task_id)

    if task is None:
        return {"status": "not_found", "task_id": task_id}

    if employee and (_field(task, "assigned_to") or "").lower() != employee.strip().lower():
        return {"status": "not_found", "task_id": task_id}

    row = {
        column: _field(task, column)
        for column in task_fields.field_columns(normalized)
    }

    return {
        "status": "success",
        "task_id": task_id,
        "field": normalized,
        "columns": list(task_fields.field_columns(normalized)),
        "value": task_fields.format_field_value(normalized, row),
    }


def _find_payload(find) -> dict:
    """The search criteria of a request, in a JSON friendly shape."""

    return {
        "task_id": getattr(find, "task_id", None),
        "task_title": getattr(find, "task_title", None),
        "assigned_to": getattr(find, "assigned_to", None),
    }


class _FindCriteria:
    """A small stand-in for ``TaskUpdateFind`` built from a draft."""

    def __init__(self, task_id=None, task_title=None, assigned_to=None):
        self.task_id = task_id
        self.task_title = task_title
        self.assigned_to = assigned_to


def _pending_update_response(draft: dict, task_id, message: str) -> dict:
    """The reply used while an update is still waiting for details."""

    return {
        "status": "clarification_required",
        "intent": CLARIFICATION_REQUIRED,
        "stage": STAGE_CONFIRM_UPDATE,
        "draft": draft,
        "task_id": task_id,
        "message": message,
        "reply": message,
    }


def _resolve_pending_target(
    draft: dict,
    new_request,
    *,
    task_id: int | None = None,
    employee: str | None = None,
    user_input: str = "",
):
    """Work out which task a pending update belongs to.

    Returns ``(task_id, None)`` when the task is known and
    ``(None, response)`` when the employee still has to choose.
    """

    draft = draft or {}

    stored = draft.get("find") or {}
    criteria = _FindCriteria(
        task_id=stored.get("task_id"),
        task_title=stored.get("task_title"),
        assigned_to=stored.get("assigned_to"),
    )

    if new_request is not None:
        newest = _find_payload(new_request.find)
        if newest.get("task_id"):
            criteria.task_id = newest["task_id"]
        if newest.get("task_title"):
            criteria.task_title = newest["task_title"]
        if newest.get("assigned_to"):
            criteria.assigned_to = newest["assigned_to"]

    if criteria.task_id is None and task_id is not None:
        criteria.task_id = task_id

    owner = _owner_for(criteria, employee)

    # The candidates that were already offered to the employee.
    candidates = draft.get("candidates") or []

    if candidates:
        if len(candidates) == 1:
            return candidates[0].get("id"), None

        selection = UPDATE_ENGINE.resolve_selection(
            {"candidates": candidates, "changes": {}}, user_input
        )

        if selection is not None:
            return selection["task_id"], None

        return None, _task_selection_response(
            "I found more than one matching task. Which one should I update?",
            candidates,
            changes=draft.get("changes") or {},
        )

    matches = _find_update_task(criteria, criteria.task_id, owner)

    if len(matches) == 1:
        return _field(matches[0], "id"), None

    if len(matches) > 1:
        return None, _task_selection_response(
            "I found more than one matching task. Which one should I update?",
            matches,
            changes=draft.get("changes") or {},
        )

    all_tasks = get_all_tasks()

    if len(all_tasks) == 1:
        return _field(all_tasks[0], "id"), None

    return None, _task_selection_response(
        "I couldn't find a matching task, so nothing was changed.",
        all_tasks,
        changes=draft.get("changes") or {},
    )


def _status_progress_conflict(updates: dict) -> bool:
    """True when the stated status and progress contradict each other."""

    status = updates.get("status")
    progress = updates.get("progress")

    if status == "COMPLETED" and progress is not None and progress != 100:
        return True

    return False


def _apply_update(
    task_id: int | None,
    updates: dict,
    *,
    employee: str | None = None,
    user_input: str = "",
    staged: bool = False,
) -> dict:
    """Validate, execute and report one update."""

    if task_id is None:
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": "I could not work out which task to update.",
            "reply": "I could not work out which task to update.",
        }

    task = get_task(task_id)

    if task is None:
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": "Task not found.",
            "reply": "Task not found.",
        }

    if employee and (_field(task, "assigned_to") or "").lower() != employee.strip().lower():
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": (
                "That task belongs to somebody else, so I did not change it."
            ),
            "reply": (
                "That task belongs to somebody else, so I did not change it."
            ),
        }

    try:
        validated = validate_updates(updates)
    except OperationError as error:
        reply = f"I could not apply that change: {error}"
        return {
            "status": "clarification_required",
            "intent": CLARIFICATION_REQUIRED,
            "stage": STAGE_CONFIRM_UPDATE,
            "message": reply,
            "reply": reply,
        }

    # The employee contradicted a business rule (a completed task must be at
    # 100%). The corrected values are proposed and confirmed first.
    conflict = _status_progress_conflict(validated)

    if conflict and not staged:
        corrected = _sync_update_status_progress(task, dict(validated))
        reply = (
            "A completed task has to be at 100% progress, so I would set the "
            "progress to 100%. Should I apply that change?"
        )
        return {
            "status": "confirmation_required",
            "intent": UPDATE_TASK,
            "stage": STAGE_CONFIRM_UPDATE,
            "draft": UPDATE_ENGINE.pending_draft(
                _field(task, "id"), corrected, task=serialize_task(task)
            ),
            "task_id": _field(task, "id"),
            "message": reply,
            "reply": reply,
        }

    validated = _sync_update_status_progress(task, validated)

    # The generated statement always carries the task id and the ownership
    # condition, and never a value written by the model.
    operation = build_update_operation(
        _field(task, "id"), validated, intent=UPDATE_TASK,
        owner=_field(task, "assigned_to")
    )

    try:
        validate_operation(operation)
    except OperationError as error:
        reply = f"I could not apply that change: {error}"
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": reply,
            "reply": reply,
        }

    confirmation = _update_confirmation(user_input, validated, staged=staged)

    result = run_sql_operation(operation, confirmation=confirmation)

    if result.get("status") != "success":
        message = result.get("message") or "The update could not be applied."
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": message,
            "reply": message,
        }

    updated_task = get_task(_field(task, "id"))

    if updated_task is None:
        return {
            "status": "error",
            "intent": UPDATE_TASK,
            "message": "Task not found.",
            "reply": "Task not found.",
        }

    payload = serialize_task(updated_task)
    changed = _describe_changes(validated)

    reply = (
        f"Updated task #{payload.get('id')} — "
        f"{payload.get('task_title') or 'Untitled task'}."
    )
    if changed:
        reply = f"{reply} Changed {changed}."

    return {
        "status": "success",
        "intent": UPDATE_TASK,
        "stage": "update",
        "message": reply,
        "reply": reply,
        "task": payload,
        "changes": validated,
    }


# --- read flows -------------------------------------------------------------------


def process_get(
    user_input: str,
    task_id: int | None = None,
    *,
    employee: str | None = None,
    context_task_id: int | None = None,
) -> dict:
    """Read one existing task. Nothing is written to the database.

    A field question is answered with that field only, read fresh from
    PostgreSQL. A general read still returns the task summary. The active task
    of the conversation is used only when the message points back to it.
    """

    try:
        query = extractor.extract_query(user_input)
    except RuntimeError as error:
        return {
            "status": "error",
            "intent": GET_TASK,
            "message": str(error),
            "reply": str(error),
        }

    query = _trusted_task_id(user_input, query)

    requested_field = task_fields.resolve_requested_field(user_input)

    if requested_field is None:
        requested_field = task_fields.normalize_field(
            getattr(query, "requested_field", None)
        )

    explicit_task_id = task_id

    if explicit_task_id is None and _mentioned_task_id(
        user_input, getattr(query.find, "task_id", None)
    ):
        explicit_task_id = query.find.task_id

    if explicit_task_id is None and getattr(query.find, "task_id", None) is not None:
        # An id the extraction model produced but the employee never wrote.
        query.find.task_id = None

    context_target = _context_target_for(
        user_input,
        query.find,
        context_task_id,
        filters=query.filters,
        requested_field=requested_field,
    )

    owner = _owner_for(query.find, employee)

    if not owner:
        owner = _owner_for(query.filters, employee)

    agent_field = (
        None
        if requested_field == task_fields.FULL_DETAILS
        else requested_field
    )

    result = TaskAgent().run(
        ReadRequest(
            message=user_input,
            task_id=explicit_task_id,
            context_task_id=context_target,
            employee=employee,
            requested_field=agent_field,
            use_context=context_target is not None,
            search_fn=lambda: _find_query_tasks(
                query, explicit_task_id, owner
            ),
            context_fn=lambda: {"active_task_id": context_target},
            task_fn=lambda task_id, employee=None: get_task(task_id),
            field_fn=_read_task_field,
        )
    )

    status = result.get("status")

    if status == "task_selection_required":
        return _task_selection_response(
            "I found more than one matching task. Which one do you mean?",
            result.get("tasks") or [],
            action="read",
            intent=GET_TASK,
            stage=STAGE_SELECT_TASK,
        )

    if status == "not_found":
        reply = "I couldn't find a matching task."
        return {
            "status": "not_found",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": None,
            "tasks": [],
        }

    if status == "clarification_required":
        reply = result.get("message") or (
            "Which task are you referring to? Please provide its task ID."
        )
        return {
            "status": "clarification_required",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": None,
            "tasks": [],
        }

    if status != "success":
        reply = result.get("message") or "The task could not be read."
        return {
            "status": "error",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
        }

    if result.get("task") is not None:
        task = serialize_task(result["task"])

        reply = (
            f"Task #{task.get('id')} — {task.get('task_title') or 'Untitled task'} "
            f"is {task.get('status') or 'PENDING'} at {task.get('progress') or 0}%."
        )

        return {
            "status": "success",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": task,
            "tasks": [task],
        }

    stored = get_task(result.get("task_id"))

    if stored is None or (
        employee
        and (_field(stored, "assigned_to") or "").lower()
        != employee.strip().lower()
    ):
        reply = "I couldn't find a matching task."
        return {
            "status": "not_found",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": None,
            "tasks": [],
        }

    task = serialize_task(stored)
    value = result.get("value")

    return {
        "status": "success",
        "intent": GET_TASK,
        "message": value,
        "reply": value,
        "task": task,
        "tasks": [task],
        "requested_field": result.get("field") or requested_field,
        "value": value,
    }


def read_task(task_id: int, *, employee: str | None = None) -> dict:
    """Read one task by id.

    No extraction and no write: this is the plain REST read path.

    The answer always carries the same keys, also when the task does not
    exist: ``task`` is ``None`` and ``tasks`` is empty, so a client can read
    ``body["task"]`` without a ``KeyError``.
    """

    task = get_task(task_id)

    if task is None:
        reply = f"I couldn't find task #{task_id}."
        return {
            "status": "not_found",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": None,
            "tasks": [],
        }

    if employee and (_field(task, "assigned_to") or "").lower() != employee.strip().lower():
        reply = "That task belongs to somebody else."
        return {
            "status": "not_found",
            "intent": GET_TASK,
            "message": reply,
            "reply": reply,
            "task": None,
            "tasks": [],
        }

    payload = serialize_task(task)

    reply = (
        f"Task #{payload.get('id')} — "
        f"{payload.get('task_title') or 'Untitled task'} is "
        f"{payload.get('status') or 'PENDING'} at {payload.get('progress') or 0}%."
    )

    return {
        "status": "success",
        "intent": GET_TASK,
        "message": reply,
        "reply": reply,
        "task": payload,
    }


def process_list(
    user_input: str,
    *,
    employee: str | None = None,
) -> dict:
    """List the existing tasks that match the request."""

    try:
        query = extractor.extract_query(user_input)
    except RuntimeError as error:
        return {
            "status": "error",
            "intent": LIST_TASKS,
            "message": str(error),
            "reply": str(error),
        }

    query = _trusted_task_id(user_input, query)

    filters = query.filters

    owner = _owner_for(query.find, employee)

    if not owner:
        owner = _owner_for(filters, employee)

    # An employee may only read their own tasks. When the message explicitly
    # names somebody else, the two conditions are combined, so the answer is
    # empty instead of showing the wrong rows.
    requested_assignee = _clean_reference(filters.assigned_to)
    assigned_to = None

    if (
        requested_assignee
        and owner
        and requested_assignee.lower() != owner.lower()
    ):
        assigned_to = requested_assignee

    operation = build_select_operation(
        status=filters.status,
        assigned_to=assigned_to,
        owner=owner,
        title_contains=filters.title_contains,
        intent=LIST_TASKS,
    )

    try:
        validate_operation(operation)
    except OperationError as error:
        return {
            "status": "error",
            "intent": LIST_TASKS,
            "message": str(error),
            "reply": str(error),
        }

    result = run_sql_operation(operation)

    if result.get("status") != "success":
        message = result.get("message") or "The tasks could not be loaded."
        return {
            "status": "error",
            "intent": LIST_TASKS,
            "message": message,
            "reply": message,
        }

    rows = result.get("rows") or []
    tasks = [serialize_task(row) for row in rows]

    if filters.title_contains:
        tasks = task_search.rank(
            tasks, filters.title_contains, fields=("task_title",)
        )

    if not tasks:
        reply = "I couldn't find any task that matches."
    else:
        lines = "\n".join(_task_choice_line(task) for task in tasks)
        reply = f"I found {len(tasks)} task(s):\n{lines}"

    return {
        "status": "success",
        "intent": LIST_TASKS,
        "message": reply,
        "reply": reply,
        "count": len(tasks),
        "tasks": tasks,
    }


# --- delete flow -------------------------------------------------------------------


def process_delete(
    user_input: str,
    task_id: int | None = None,
    *,
    employee: str | None = None,
    confirm_delete: bool = False,
    draft: dict | None = None,
) -> dict:
    """Delete a task. An explicit confirmation is always required."""

    # The confirmation survives the turns of the conversation.
    confirmed = (
        confirm_delete
        or _has_delete_confirmation(user_input)
        or bool((draft or {}).get("confirmed"))
    )

    if task_id is None:
        # 1. The employee may already have picked a task in an earlier turn.
        candidates = (draft or {}).get("candidates") or []

        if candidates:
            selection = UPDATE_ENGINE.resolve_selection(
                {"candidates": candidates, "changes": {}}, user_input
            )

            if selection is not None:
                task_id = selection["task_id"]
            elif len(candidates) == 1:
                task_id = candidates[0].get("id")
            else:
                response = _task_selection_response(
                    "Which task should I delete?",
                    candidates,
                    action="delete",
                    intent=DELETE_TASK,
                    stage=STAGE_CONFIRM_DELETE,
                )
                response["draft"]["confirmed"] = confirmed
                return response

        # 2. Otherwise work the target out of the message itself.
        if task_id is None:
            query = _extract_query_request(user_input)

            if query is not None:
                matches = _find_query_tasks(
                    query, None, _owner_for(query.find, employee)
                )
            else:
                matches = []

            if not matches:
                response = _task_selection_response(
                    "Which task should I delete?",
                    get_all_tasks(),
                    action="delete",
                    intent=DELETE_TASK,
                    stage=STAGE_CONFIRM_DELETE,
                )
                response["draft"]["confirmed"] = confirmed
                return response

            if len(matches) > 1:
                response = _task_selection_response(
                    "I found more than one matching task. "
                    "Which one should I delete?",
                    matches,
                    action="delete",
                    intent=DELETE_TASK,
                    stage=STAGE_CONFIRM_DELETE,
                )
                response["draft"]["confirmed"] = confirmed
                return response

            task_id = _field(matches[0], "id")

    task = get_task(task_id)

    if task is None:
        return {
            "status": "error",
            "intent": DELETE_TASK,
            "message": "Task not found.",
            "reply": "Task not found.",
        }

    if employee and (_field(task, "assigned_to") or "").lower() != employee.strip().lower():
        return {
            "status": "error",
            "intent": DELETE_TASK,
            "message": (
                "That task belongs to somebody else, so I did not delete it."
            ),
            "reply": (
                "That task belongs to somebody else, so I did not delete it."
            ),
        }

    if not confirmed:
        phrase = confirmation_phrase("DELETE")
        reply = (
            f"Deleting task #{_field(task, 'id')} requires confirmation. "
            f"Reply '{phrase}' to proceed."
        )
        return {
            "status": "confirmation_required",
            "intent": DELETE_TASK,
            "stage": STAGE_CONFIRM_DELETE,
            "task_id": _field(task, "id"),
            "draft": {
                **UPDATE_ENGINE.pending_draft(
                    _field(task, "id"), {}, task=serialize_task(task)
                ),
                "confirmed": False,
            },
            "message": reply,
            "reply": reply,
        }

    operation = build_delete_operation(
        _field(task, "id"), intent=DELETE_TASK,
        owner=_field(task, "assigned_to")
    )

    try:
        validate_operation(operation)
    except OperationError as error:
        return {
            "status": "error",
            "intent": DELETE_TASK,
            "message": str(error),
            "reply": str(error),
        }

    result = run_sql_operation(
        operation, confirmation=confirmation_phrase("DELETE")
    )

    if result.get("status") != "success":
        message = result.get("message") or "The task could not be deleted."
        return {
            "status": "error",
            "intent": DELETE_TASK,
            "message": message,
            "reply": message,
        }

    reply = f"Task #{_field(task, 'id')} deleted successfully."

    return {
        "status": "success",
        "intent": DELETE_TASK,
        "message": reply,
        "reply": reply,
    }


def _has_delete_confirmation(user_input: str) -> bool:
    phrase = confirmation_phrase("DELETE") or ""
    return phrase.lower() in (user_input or "").lower()


# --- general conversation ------------------------------------------------------------


def process_general_query(user_input: str) -> dict:
    """Answer a message that is not a task operation."""

    reply = (
        "I can create, update, read, list and delete tasks. "
        "Try 'Create a task to prepare the business report', "
        "'Update my business report task progress to 70%', "
        "'What is the current status of my database task?' or "
        "'Show all my pending tasks'."
    )

    return {
        "status": "success",
        "intent": GENERAL_QUERY,
        "message": reply,
        "reply": reply,
    }


# --- conversation memory ------------------------------------------------------------


def _memory_identity(user_id: str | None, employee: str | None) -> str | None:
    """The user a memory belongs to.

    The employee of the request is used when no explicit user id is given.
    """

    for candidate in (user_id, employee):
        text = (candidate or "").strip()

        if text:
            return text

    return None


def _with_memory(
    user_input: str,
    *,
    conversation_id: str | None,
    user_id: str | None,
    employee: str | None,
    rewrite: bool = True,
    stage: str | None = None,
    draft: dict | None = None,
    run,
):
    """Run the agent flow with the memory of the conversation around it.

    The memory is read before the flow runs, a follow-up message is rewritten
    into a standalone instruction, and a successful operation is stored back
    in the memory afterwards.

    ``rewrite`` is off for the turns of a running conversation (the answer to
    a question of the assistant): those messages belong to the draft of the
    conversation and are never rewritten.
    """

    transcript = (user_input or "").strip()

    memory = _load_memory(user_id, employee, conversation_id)

    instruction = transcript
    rewrite_status = "not_needed"

    if rewrite:
        instruction, rewrite_status = _rewrite_message(transcript, memory)

    # A clarification that was stored before a restart is restored, so the
    # answer ("the second one", "Task #614", "yes") still belongs to it.
    stage, draft = _restore_conversation_state(
        memory, stage, draft, instruction
    )

    context_task_id = _active_task_id(memory)

    if rewrite_status == "rejected_task_id":
        context_task_id = None

    result = run(instruction, stage, draft, context_task_id)

    _remember_turn(
        user_id=user_id,
        employee=employee,
        conversation_id=conversation_id,
        user_message=transcript,
        result=result,
    )

    return result


def _displayed_candidates(result: dict) -> list[dict]:
    """The candidates that were shown, reduced to what a selection needs."""

    draft = (result or {}).get("draft") or {}
    candidates = []

    for candidate in (draft.get("candidates") or [])[:MAX_SELECTION_CANDIDATES]:
        if not isinstance(candidate, dict) or candidate.get("id") is None:
            continue

        candidates.append(
            {
                "id": candidate.get("id"),
                "task_title": candidate.get("task_title"),
                "assigned_to": candidate.get("assigned_to"),
                "status": candidate.get("status"),
            }
        )

    return candidates


def _clarification_of_result(result: dict) -> dict:
    """The clarification that is waiting for the employee."""

    draft = (result or {}).get("draft") or {}
    stage = (result or {}).get("stage")

    if not stage or stage not in UPDATE_STAGES:
        return {}

    if not (
        draft.get("candidates")
        or draft.get("changes")
        or draft.get("task_id")
    ):
        return {}

    return {
        "stage": stage,
        "intent": (result or {}).get("intent"),
        "action": draft.get("action")
        or (
            "delete"
            if (result or {}).get("intent") == DELETE_TASK
            else "update"
        ),
        "changes": dict(draft.get("changes") or {}),
        "find": dict(draft.get("find") or {}),
        "task_id": draft.get("task_id"),
    }


def _looks_like_selection(
    message: str, candidates: list, action: str = "update"
) -> bool:
    """Does this message answer "which task do you mean?"""

    text = (message or "").strip()

    if not text or not candidates:
        return False

    # A message that states a new change is a new operation, not a selection.
    if NEW_OPERATION_PATTERN.search(text):
        return False

    intent = detect_intent(text).name

    if intent in (CREATE_TASK, LIST_TASKS):
        return False

    if intent == DELETE_TASK and action != "delete":
        return False

    # Messages with a selection signal ("task 2", "the second one") and
    # messages that name a candidate by its title or keywords ("css task")
    # both count as an answer to "which task do you mean?".
    return (
        UPDATE_ENGINE.resolve_selection(
            {"candidates": candidates, "changes": {}}, text
        )
        is not None
    )


def _restore_conversation_state(
    memory: dict | None,
    stage: str | None,
    draft: dict | None,
    message: str,
):
    """Bring a stored clarification back into the running conversation.

    Only a fresh turn is restored: an explicit stage sent by the client
    always wins. A confirmation is restored for an answer ("yes", "no"), a
    selection for a selection message ("the second one", "Task #614").
    """

    if stage not in FRESH_STAGES:
        return stage, draft

    context = (memory or {}).get("context") or {}
    clarification = context.get("clarification") or {}
    saved_stage = clarification.get("stage")
    candidates = context.get("candidates") or []

    if saved_stage not in UPDATE_STAGES:
        return stage, draft

    action = clarification.get("action") or "update"

    is_confirmation = saved_stage == STAGE_CONFIRM_UPDATE and (
        _is_affirmation(message) or _is_negative(message)
    )

    if not is_confirmation and not _looks_like_selection(
        message, candidates, action
    ):
        return stage, draft

    restored = dict(draft or {})

    if candidates:
        restored.setdefault("candidates", candidates)

    for key in ("intent", "action", "changes", "find", "task_id"):
        value = clarification.get(key)

        if value not in (None, {}, []):
            restored.setdefault(key, value)

    return saved_stage, restored


def _load_memory(
    user_id: str | None,
    employee: str | None,
    conversation_id: str | None,
) -> dict | None:
    """The stored memory of the conversation, or ``None``."""

    identity = _memory_identity(user_id, employee)
    conversation = (conversation_id or "").strip()

    if not identity or not conversation:
        return None

    try:
        memory = get_conversation_memory(identity, conversation)

        if memory is None:
            return None

        # The rewriting step always receives the latest ten messages of this
        # conversation, in chronological order.
        memory["context"]["history"] = get_recent_messages(
            identity, conversation
        )

        return memory
    except Exception as error:
        logger.warning("The conversation memory could not be read: %s", error)
        return None


def _rewrite_message(message: str, memory: dict | None) -> tuple[str, str]:
    """Rewrite a follow-up message into a standalone instruction.

    The original message is kept when the message is already standalone, when
    the memory is not enough, or when the rewriting fails. The status tells
    the caller whether the model named an unknown task, in which case the
    conversation's active task must not silently replace it.
    """

    if not memory:
        return message, "not_needed"

    try:
        return rewrite_query_with_status(message, memory=memory)
    except Exception as error:
        logger.warning("The message could not be rewritten: %s", error)
        return message, "failed"


def _task_id_of(result: dict):
    """The task a successful answer is about."""

    task = result.get("task")

    if isinstance(task, dict) and task.get("id") is not None:
        return task["id"]

    if result.get("task_id") is not None:
        return result["task_id"]

    return None


def _pending_operation(result: dict) -> dict | None:
    """The change the employee asked for, when the task is still missing.

    The assistant asks which task is meant when the request is not clear
    enough. The change the employee asked for is kept, so the answer to that
    question completes the change instead of starting a new one.
    """

    draft = (result or {}).get("draft") or {}

    changes = draft.get("changes") or {}

    if not changes:
        return None

    return {
        "changes": dict(changes),
        "find": dict(draft.get("find") or {}),
        "task_id": draft.get("task_id"),
    }


def _remember_turn(
    *,
    user_id: str | None,
    employee: str | None,
    conversation_id: str | None,
    user_message: str,
    result: dict,
):
    """Store what this turn taught the conversation.

    Only a successful operation changes the active task and the last action:
    a failed operation is never stored as if it had worked. The turns of the
    conversation itself are kept in a short history.
    """

    identity = _memory_identity(user_id, employee)
    conversation = (conversation_id or "").strip()

    if not identity or not conversation:
        return

    reply = (result or {}).get("reply") or (result or {}).get("message") or ""

    turns = [{"role": "user", "text": user_message}]

    if reply:
        turns.append({"role": "assistant", "text": reply})

    try:
        append_history(identity, conversation, turns)
    except Exception as error:
        logger.warning("The conversation history could not be stored: %s", error)
        return

    pending = _pending_operation(result)
    candidates = _displayed_candidates(result)
    clarification = _clarification_of_result(result)

    if (result or {}).get("status") != "success":
        context = {}

        # The change the employee asked for is kept until it is applied.
        if pending:
            context["pending"] = {
                **pending,
                "instruction": user_message,
            }

        # The displayed candidates and the clarification survive a restart,
        # so "the second one" can still be resolved later.
        if candidates:
            context["candidates"] = candidates

        if clarification:
            context["clarification"] = clarification

        if not context:
            # The turn started something else, so an older clarification is
            # not waiting any more. A failed operation never changes the
            # active task.
            context = {
                "pending": {},
                "candidates": [],
                "clarification": {},
            }

        try:
            save_conversation_memory(
                identity, conversation, context=context
            )
        except Exception as error:
            logger.warning(
                "The pending operation could not be stored: %s", error
            )

        return

    intent = (result or {}).get("intent")
    task_id = _task_id_of(result)

    # A completed interaction closes the pending clarification.
    cleared = {"pending": {}, "candidates": [], "clarification": {}}

    try:
        if intent == DELETE_TASK:
            # The task is gone, so it is not the active one any more.
            update_active_task(
                identity, conversation, None, employee=employee,
                last_action=intent, context=cleared,
            )
            return

        if task_id is None:
            save_conversation_memory(
                identity, conversation, last_action=intent,
                context=cleared,
            )
            return

        update_active_task(
            identity,
            conversation,
            task_id,
            employee=employee,
            task=result.get("task"),
            last_action=intent,
            context=cleared,
        )
    except Exception as error:
        logger.warning("The conversation memory could not be stored: %s", error)


# --- entry points ---------------------------------------------------------------------


AFFIRMATIVE_PHRASES = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "correct", "confirmed",
    "confirm", "sure", "save", "save it", "looks good", "proceed",
    "continue", "go ahead", "haan", "han", "theek hai", "thik hai",
    "sahi hai", "sahi", "ha",
}

NEGATIVE_PHRASES = {
    "no", "nope", "nah", "not", "wrong", "incorrect", "change", "edit",
    "cancel", "stop", "redo", "nahi", "nahin", "galat",
}


def _normalize_voice_text(text: str) -> str:
    return " ".join(
        (text or "").lower().replace(".", " ").replace(",", " ").split()
    )


def _is_affirmation(text: str) -> bool:
    normalized = _normalize_voice_text(text)

    if normalized in AFFIRMATIVE_PHRASES:
        return True

    words = set(normalized.split())
    if words & NEGATIVE_PHRASES:
        return False

    return any(
        normalized == phrase or normalized.startswith(f"{phrase} ")
        for phrase in ("yes", "yeah", "ok", "okay", "confirm", "correct", "haan")
    )


def _is_negative(text: str) -> bool:
    normalized = _normalize_voice_text(text)

    if normalized in NEGATIVE_PHRASES:
        return True

    return bool(set(normalized.split()) & NEGATIVE_PHRASES)


def _display_value(name: str, value) -> str:
    if value is None or value == "":
        return "-"

    if name.endswith("_date"):
        try:
            return date.fromisoformat(str(value)).strftime("%d %B %Y")
        except ValueError:
            return str(value)

    if name.endswith("_time"):
        return str(value)

    return str(value)


def _draft_summary(draft: dict) -> str:
    lines = []
    for name in FIELD_ORDER:
        label = FIELD_LABELS.get(name, name)
        lines.append(f"• {label}: {_display_value(name, (draft or {}).get(name))}")
    return "\n".join(lines)


def _task_in_context(
    stage: str | None,
    draft: dict | None,
    task_id: int | None = None,
) -> bool | None:
    """Does the conversation already identify a task?

    ``True``  - yes, a task is known,
    ``False`` - no, and the conversation is about an existing task, so the
    employee has to be asked which one is meant,
    ``None``  - nothing is known about the conversation yet.
    """

    if task_id is not None:
        return True

    if stage not in UPDATE_STAGES:
        return None

    draft = draft or {}
    find = draft.get("find") or {}

    return bool(
        draft.get("task_id")
        or draft.get("candidates")
        or find.get("task_id")
    )


def _start_create_flow(user_input: str) -> dict:
    """Begin a brand new task, whatever the conversation was doing."""

    return _run_create_flow(
        user_input,
        stage="start",
        draft=None,
        answers=None,
        reminder_text=None,
        confirm=False,
    )


def process_task(
    user_input: str,
    task_id: int | None = None,
    confirm_delete: bool = False,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
    employee: str | None = None,
    context_task_id: int | None = None,
):
    """Handle one turn of the text/voice entry points."""

    is_start = stage in (None, "", "start")

    if not user_input or not user_input.strip():
        if is_start:
            return {"status": "error", "message": "Input cannot be empty."}

    in_context = _task_in_context(stage, draft, task_id)

    if (
        in_context is None
        and context_task_id is not None
        and (
            task_fields.resolve_requested_field(user_input) is not None
            or REFERENCE_TO_ACTIVE_TASK_PATTERN.search(user_input or "")
        )
    ):
        in_context = True

    intent = detect_intent(
        user_input,
        task_in_context=in_context,
    )

    # 1. An explicit request always wins over a pending conversation.
    #    A creation never looks an existing task up, and an update never
    #    starts a new task draft.
    if stage in UPDATE_STAGES and is_explicit_create_request(user_input):
        return _start_create_flow(user_input)

    if stage in CREATE_STAGES and is_explicit_update_request(user_input):
        return process_update(
            user_input,
            task_id=task_id,
            employee=employee,
            context_task_id=context_task_id,
        )

    # 2. Continuation of the "change an existing task" conversation.
    if stage in UPDATE_STAGES:
        if intent.name == CREATE_TASK and not _looks_like_selection(
            user_input,
            (draft or {}).get("candidates") or [],
            (draft or {}).get("action") or "update",
        ):
            # A new task description is a new operation, never an answer to
            # a pending read/update selection. Without this, a creation that
            # mentions "deadline", "progress" or "status" is mistaken for a
            # selection and the conversation loops.
            return _start_create_flow(user_input)

        if stage == STAGE_SELECT_TASK and (draft or {}).get("action") == "read":
            # A read found several candidates: the employee now names the one
            # to show. The candidates come from the previous answer.
            selection = UPDATE_ENGINE.resolve_selection(draft, user_input)

            if selection is None:
                return _task_selection_response(
                    "I still need to know which task to read.",
                    (draft or {}).get("candidates") or [],
                    action="read",
                    intent=GET_TASK,
                    stage=STAGE_SELECT_TASK,
                )

            return read_task(selection["task_id"], employee=employee)

        if stage == STAGE_CONFIRM_DELETE:
            return process_delete(
                user_input,
                task_id=(draft or {}).get("task_id") or task_id,
                employee=employee,
                confirm_delete=confirm_delete,
                draft=draft,
            )

        if intent.name == DELETE_TASK:
            # An explicit deletion is never swallowed by a pending update.
            return process_delete(
                user_input,
                task_id=task_id,
                employee=employee,
                confirm_delete=confirm_delete,
                draft=draft,
            )

        return process_update(
            user_input,
            task_id=task_id,
            employee=employee,
            stage=stage,
            draft=draft,
            confirm=confirm,
            context_task_id=context_task_id,
        )

    # 3. Continuation of the "build a new task" conversation.
    if not is_start:
        return _run_create_flow(
            user_input,
            stage=stage,
            draft=draft,
            answers=answers,
            reminder_text=reminder_text,
            confirm=confirm,
        )

    if intent.name == DELETE_TASK:
        return process_delete(
            user_input,
            task_id=task_id,
            employee=employee,
            confirm_delete=confirm_delete,
        )

    if intent.name == UPDATE_TASK:
        return process_update(
            user_input,
            task_id=task_id,
            employee=employee,
            context_task_id=context_task_id,
        )

    if intent.name == GET_TASK:
        return process_get(
            user_input,
            task_id=task_id,
            employee=employee,
            context_task_id=context_task_id,
        )

    if intent.name == LIST_TASKS:
        return process_list(user_input, employee=employee)

    if intent.name == CREATE_TASK:
        return _start_create_flow(user_input)

    if intent.name == CLARIFICATION_REQUIRED:
        # The employee talks about a task the conversation has not
        # identified yet: ask which one is meant instead of guessing.
        return process_update(
            user_input,
            task_id=task_id,
            employee=employee,
            context_task_id=context_task_id,
        )

    return process_general_query(user_input)


def process_chat_turn(
    user_input: str,
    *,
    task_id: int | None = None,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
    confirm_delete: bool = False,
    employee: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
):
    """Handle one turn of the live chat conversation.

    The turn is prepared before the agent runs:

    1. the memory of the conversation is read from PostgreSQL,
    2. a follow-up message is rewritten into a standalone instruction,
    3. the (rewritten) instruction goes through the existing agent flow,
    4. a successful operation is stored back in the memory.

    The answer itself is the answer of the existing flow: the memory and the
    rewriting never change what the agent does, they only make a follow-up
    message understandable.
    """

    transcript = (user_input or "").strip()

    return _with_memory(
        transcript,
        conversation_id=conversation_id,
        user_id=user_id,
        employee=employee,
        stage=stage,
        draft=draft,
        # A turn of a running conversation answers a question of the
        # assistant, so it is never rewritten.
        rewrite=stage in FRESH_STAGES,
        run=lambda instruction, restored_stage, restored_draft, restored_task_id: _run_chat_turn(
            instruction,
            task_id=task_id,
            stage=restored_stage,
            draft=restored_draft,
            answers=answers,
            reminder_text=reminder_text,
            confirm=confirm,
            confirm_delete=confirm_delete,
            employee=employee,
            context_task_id=restored_task_id,
        ),
    )


def _run_chat_turn(
    user_input: str,
    *,
    task_id: int | None = None,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
    confirm_delete: bool = False,
    employee: str | None = None,
    context_task_id: int | None = None,
):
    """The conversation flow itself, without the memory of the conversation."""

    transcript = (user_input or "").strip()
    draft = draft or {}

    # Backwards compatibility with the older staged CREATE flow.
    if stage == "clarify":
        stage = STAGE_FIELDS

    # The "change an existing task" conversation has its own stages.
    if stage in UPDATE_STAGES:
        return process_task(
            transcript,
            task_id=task_id,
            # A delete is only confirmed by the explicit delete confirmation,
            # never by the generic "confirm" of the CREATE flow.
            confirm_delete=confirm_delete,
            stage=stage,
            draft=draft,
            confirm=confirm,
            employee=employee,
            context_task_id=context_task_id,
        )

    if stage in (None, "", "start", STAGE_UPDATE):
        # A new turn may be CREATE, UPDATE, GET, LIST or DELETE. Share the
        # same intent detection as the text/voice entry points.
        # ``STAGE_UPDATE`` is the stage of a finished update, so the next
        # message starts a new turn.
        return process_task(
            transcript,
            task_id=task_id,
            stage="start",
            employee=employee,
            context_task_id=context_task_id,
        )

    if stage in CREATE_STAGES and is_explicit_update_request(transcript):
        # The employee asks to change an existing task in the middle of a
        # creation conversation: the new task draft is left behind, and no
        # new-task extraction is run for an explicit update request.
        return process_task(
            transcript,
            task_id=task_id,
            stage="start",
            employee=employee,
            context_task_id=context_task_id,
        )

    if not transcript and stage not in (
        STAGE_CONFIRM_FIELDS,
        STAGE_CONFIRM_REMINDER,
    ):
        return {
            "status": "error",
            "message": "Input cannot be empty.",
            "reply": "Please send a reply so I can continue.",
        }

    engine = TaskDraftEngine(extractor)

    try:
        if stage == STAGE_FIELDS:
            result = engine.answer(draft, transcript)

        elif stage == STAGE_CONFIRM_FIELDS:
            if answers:
                result = engine.apply(draft, answers)
            elif confirm or _is_affirmation(transcript):
                result = engine.confirm_fields(draft, True)
            elif _is_negative(transcript):
                return {
                    "status": "confirmation_required",
                    "stage": STAGE_CONFIRM_FIELDS,
                    "draft": draft,
                    "message": (
                        "Okay. Tell me which field to change and its new "
                        "value, for example 'progress is 80 percent'."
                    ),
                    "reply": (
                        "Okay. Tell me which field to change and its new "
                        "value, for example 'progress is 80 percent'."
                    ),
                }
            else:
                result = engine.edit(draft, transcript)

        elif stage == STAGE_REMINDER:
            result = engine.set_reminder(
                draft, reminder_text or transcript
            )

        elif stage == STAGE_CONFIRM_REMINDER:
            if confirm or _is_affirmation(transcript):
                result = engine.confirm_reminder(draft, True)
            elif _is_negative(transcript):
                result = engine.confirm_reminder(draft, False)
            else:
                result = engine.set_reminder(draft, transcript)

        else:
            return {
                "status": "error",
                "message": f"Unknown stage '{stage}'.",
                "reply": f"Unknown stage '{stage}'.",
            }

    except (RuntimeError, ValueError) as error:
        return {
            "status": "error",
            "message": str(error),
            "reply": str(error),
        }

    return _engine_result_to_response(result)


def process_voice_turn(
    user_input: str,
    *,
    task_id: int | None = None,
    confirm_delete: bool = False,
    stage: str | None = None,
    draft: dict | None = None,
    answers: dict | None = None,
    reminder_text: str | None = None,
    confirm: bool = False,
    employee: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
):
    """Drive one conversation turn from a spoken transcript.

    The transcript is treated like the user's reply to the assistant's last
    question, so voice and text share the same one-field-at-a-time flow.
    """

    transcript = (user_input or "").strip()

    if stage in (None, "", "start"):
        return _with_memory(
            transcript,
            conversation_id=conversation_id,
            user_id=user_id,
            employee=employee,
            stage=stage,
            draft=draft,
            run=lambda instruction, restored_stage, restored_draft, restored_task_id: process_task(
                instruction,
                task_id=task_id,
                confirm_delete=confirm_delete,
                stage=restored_stage or "start",
                draft=restored_draft,
                employee=employee,
                context_task_id=restored_task_id,
            ),
        )

    return process_chat_turn(
        transcript,
        task_id=task_id,
        stage=stage,
        draft=draft,
        answers=answers,
        reminder_text=reminder_text,
        confirm=confirm,
        employee=employee,
        conversation_id=conversation_id,
        user_id=user_id,
    )


def _run_create_flow(
    user_input: str,
    *,
    stage: str | None,
    draft: dict | None,
    answers: dict | None,
    reminder_text: str | None,
    confirm: bool,
):
    engine = TaskDraftEngine(extractor)

    try:
        if stage in (None, "", "start"):
            result = engine.start(user_input)
        elif stage == "fields":
            # The conversational stage: the reply fills the missing field.
            if answers:
                result = engine.apply(draft or {}, answers)
            else:
                result = engine.answer(draft or {}, user_input)
        elif stage == "clarify":
            result = engine.apply(draft or {}, answers or {})
        elif stage == "confirm_fields":
            if answers:
                result = engine.apply(draft or {}, answers)
            else:
                result = engine.confirm_fields(draft or {}, bool(confirm))
        elif stage == "reminder":
            result = engine.set_reminder(draft or {}, reminder_text or user_input)
        elif stage == "confirm_reminder":
            if reminder_text:
                result = engine.set_reminder(draft or {}, reminder_text)
            else:
                result = engine.confirm_reminder(draft or {}, bool(confirm))
        else:
            return {"status": "error", "message": f"Unknown stage '{stage}'."}

    except RuntimeError as error:
        # Cohere/API failures surface as a normal error result.
        return {"status": "error", "message": str(error)}

    return _engine_result_to_response(result)


def _engine_result_to_response(result):
    if result.done:
        saved_task = create_task(result.record)

        return {
            "status": "success",
            "intent": CREATE_TASK,
            "message": "Task created successfully.",
            "reply": "Task created successfully.",
            "task": serialize_task(saved_task)
        }

    response = {
        "stage": result.stage,
        "draft": result.draft,
        "intent": CREATE_TASK,
    }

    if result.stage == STAGE_FIELDS:
        response["status"] = "clarification_required"
        response["missing_fields"] = result.missing_fields
        response["question"] = result.question
        response["reply"] = result.question or result.message or ""
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_FIELDS:
        response["status"] = "confirmation_required"
        response["message"] = result.message
        response["reply"] = (
            "Here is what I have so far:\n"
            f"{_draft_summary(result.draft)}\n\n"
            "Reply 'yes' to save the task, or tell me which field to change."
        )

    elif result.stage == STAGE_REMINDER:
        response["status"] = "reminder_required"
        response["question"] = result.question
        response["reply"] = result.question or result.message or ""
        if result.message:
            response["message"] = result.message

    elif result.stage == STAGE_CONFIRM_REMINDER:
        response["status"] = "reminder_confirmation_required"
        response["reminder"] = result.reminder
        response["question"] = result.message
        response["message"] = result.message
        response["reply"] = result.message or ""

    return response
