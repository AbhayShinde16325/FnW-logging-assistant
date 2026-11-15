"""Rewrite a follow-up message into a standalone instruction.

The assistant often receives a message that only makes sense together with
the previous turn:

    User: Also update its deadline to 21 November 2026.

On its own that sentence does not say *which* task is meant. This module
turns such a message into one standalone instruction:

    Update the deadline of task ID 211 to 21 November 2026.

The rewrite uses the stored conversation memory (the active task and the last
turns) and the existing LLM provider. It never touches the database and never
executes anything: it only rewords a message, and the existing agent and
tools do the rest.

Safety rules that are enforced in code, not only in the prompt:

* the task id of the rewrite must come from the memory or from the message
  itself - a model can never introduce a task id that is not known,
* every number the employee stated (a date, a time, a percentage) has to
  survive the rewrite,
* a message that is already standalone is not rewritten at all,
* when the context is not enough, or when the model fails, the original
  message is used, so the existing clarification logic takes over.
"""

from __future__ import annotations

import logging
import re

from ai.intent_detection import (
    GENERAL_PATTERN,
    LIST_PATTERN,
    detect_intent,
    is_explicit_create_request,
    is_task_reference_only,
)
from services.conversation_memory import MAX_HISTORY_ENTRIES, recent_messages

logger = logging.getLogger(__name__)

# A task id in a message: "#211", "task 211", "id 211".
TASK_ID_PATTERN = re.compile(
    r"(?:#|task\s*#?\s*|id\s*#?\s*)(\d+)", re.IGNORECASE
)

# Words that point back to the task of the previous turn.
REFERENCE_PATTERN = re.compile(
    r"\b(it|its|that|this|the same one|that one|this one)\b",
    re.IGNORECASE,
)

# The fields a follow-up message can talk about.
FIELD_PATTERN = re.compile(
    r"\b(deadline|due date|progress|status|assignee|assigned to|reminder|"
    r"title|date|time)\b",
    re.IGNORECASE,
)

# Any number of the message (a day, a year, a percentage, ...).
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

REWRITE_SYSTEM_PROMPT = """
You rewrite a follow-up message of a task-management chat into one standalone
instruction for the same assistant.

Return ONLY a JSON object:
{"rewritten": "<the standalone instruction, or null>", "needs_clarification": true|false}

Rules:
1. Use the context only to resolve references such as "it", "its", "that
   task", "the same one". The context is the only source of a task id.
2. Keep the action and every value exactly as the employee stated them
   (dates, times, percentages, names, task ids). Never change, round or drop
   a value, and never add a value that was not stated.
3. Never invent a task id, deadline, progress value, title or assignee. If
   the context does not contain the task the message refers to, return
   "rewritten": null and "needs_clarification": true.
4. If the message only names a task (for example "Task #211") and the context
   holds a pending instruction, return that pending instruction with the task
   id of the message.
5. If the message already identifies the task by id or by title, or asks for
   a new task, return it unchanged.
6. If the message is not about changing or reading a task, return
   "rewritten": null and "needs_clarification": true.
7. Write one short, imperative instruction, for example
   "Update the deadline of task ID 211 to 21 November 2026." For a read, use
   "Retrieve the <field> of task ID 211" or include the exact task title.
8. Always mention the task words ("task" or "task ID") and prefer the exact
   task id and title from the context. Do not write vague phrases such as
   "previously identified" or "the task we discussed".
9. When the context lists task candidates, "the second one", "option 2",
   "the previous one" or "the last one" refers to the candidate at that
   position. Use that candidate's task id from the context.
10. Return exactly one JSON object, with no text, Markdown or extra objects.
"""


# --- helpers ---------------------------------------------------------------------


def _task_ids(text: str) -> set[int]:
    """The task ids that are mentioned in a text."""

    return {int(value) for value in TASK_ID_PATTERN.findall(text or "")}


def _has_reference(text: str) -> bool:
    """True when the message points back to the task of an earlier turn."""

    return bool(REFERENCE_PATTERN.search(text or ""))


def _numbers(text: str) -> set[str]:
    """Every number of a text, as strings (so "25" and "25.0" differ)."""

    return set(NUMBER_PATTERN.findall(text or ""))


def _active_task(memory: dict | None) -> tuple[int | None, dict]:
    """The active task id and its snapshot from the memory."""

    if not memory:
        return None, {}

    active_task_id = memory.get("active_task_id")

    context = memory.get("context") or {}
    snapshot = context.get("active_task") or {}

    if active_task_id is None and snapshot.get("id") is not None:
        active_task_id = snapshot["id"]

    return active_task_id, snapshot


def _pending_operation(memory: dict | None) -> dict | None:
    """The change the employee asked for, when the task was still missing."""

    if not memory:
        return None

    pending = (memory.get("context") or {}).get("pending") or {}

    instruction = str(pending.get("instruction") or "").strip()

    if not instruction:
        return None

    return {
        "instruction": instruction,
        "changes": pending.get("changes") or {},
    }


def _pending_instruction(memory: dict | None) -> str | None:
    """The change the employee asked for, when the task was still missing."""

    pending = _pending_operation(memory)

    return pending["instruction"] if pending else None


def _pending_values(memory: dict | None) -> set[str]:
    """The numbers of the values of the pending change.

    Only the values of the change are protected (a percentage, a date). The
    wording of the pending instruction may name another task, and that name
    is replaced by the task the employee selected.
    """

    pending = _pending_operation(memory)

    if not pending:
        return set()

    numbers: set[str] = set()

    for value in (pending.get("changes") or {}).values():
        if isinstance(value, bool) or value is None:
            continue

        if isinstance(value, (int, float)):
            numbers.add(str(int(value)) if float(value).is_integer() else str(value))
        elif isinstance(value, str):
            numbers |= _numbers(value)

    return numbers


def _history_of(memory: dict | None, history: list | None) -> list[dict]:
    """The latest messages of the conversation, in chronological order."""

    if history is not None:
        turns = list(history)
    elif memory:
        turns = list((memory.get("context") or {}).get("history") or [])
    else:
        turns = []

    return recent_messages(turns, MAX_HISTORY_ENTRIES)


def _candidates_of(memory: dict | None) -> list[dict]:
    """The tasks that were displayed to the employee."""

    if not memory:
        return []

    candidates = (memory.get("context") or {}).get("candidates") or []

    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("id") is not None
    ]


def _clarification_of(memory: dict | None) -> dict:
    """The clarification that is still waiting for an answer."""

    if not memory:
        return {}

    return (memory.get("context") or {}).get("clarification") or {}


def needs_rewrite(
    user_input: str,
    *,
    active_task_id: int | None = None,
    pending_instruction: str | None = None,
) -> bool:
    """Is this message a follow-up that has to be rewritten?

    The check is deterministic: it reuses the intent rules of the project, so
    a message that is already standalone never costs a model call.
    """

    text = (user_input or "").strip()

    if not text:
        return False

    # A message that only names a task completes the pending change of the
    # conversation: the task is missing, the change is known.
    if pending_instruction and is_task_reference_only(text):
        return True

    if active_task_id is None:
        return False

    # A new task is never built from the context of an older one.
    if is_explicit_create_request(text):
        return False

    # A request about several tasks is not about the active task.
    if LIST_PATTERN.search(text.lower()):
        return False

    # Ordinary conversation ("hi", "thanks") is not a task operation.
    if GENERAL_PATTERN.search(text.lower()):
        return False

    # A message that names its task is already standalone.
    if _task_ids(text):
        return False

    intent = detect_intent(text).name

    # A question about one task that is named by its title stays as it is.
    if intent == "GET_TASK" and not _has_reference(text):
        return False

    if intent in ("UPDATE_TASK", "DELETE_TASK"):
        return True

    # Any other message is only rewritten when it points back to the task of
    # the conversation ("its deadline", "the deadline", ...). A bare answer
    # ("50%", "yes") is left alone.
    return _has_reference(text) or bool(FIELD_PATTERN.search(text))


def _context_lines(
    active_task_id: int | None,
    snapshot: dict,
    history: list[dict],
    pending_instruction: str | None = None,
    *,
    last_action: str | None = None,
    candidates: list[dict] | None = None,
    clarification: dict | None = None,
) -> str:
    """The compact context that is sent to the model."""

    lines = ["Context:"]

    if active_task_id is not None:
        lines.append(f"- Active task id: {active_task_id}")

    if last_action:
        lines.append(f"- Last successful action: {last_action}")

    clarification = clarification or {}

    if clarification.get("stage") or clarification.get("action"):
        lines.append(
            "- Pending clarification: "
            f"stage={clarification.get('stage')}, "
            f"action={clarification.get('action')}"
        )

    if candidates:
        lines.append("Displayed task candidates (in this order):")

        for index, candidate in enumerate(candidates, start=1):
            title = candidate.get("task_title") or "(no title)"
            lines.append(
                f"  {index}. task ID {candidate.get('id')}: {title}"
            )

    if snapshot:
        if snapshot.get("task_title"):
            lines.append(f"- Active task title: {snapshot['task_title']}")
        if snapshot.get("assigned_to"):
            lines.append(f"- Active task assignee: {snapshot['assigned_to']}")
        if snapshot.get("status"):
            lines.append(f"- Active task status: {snapshot['status']}")
        if snapshot.get("progress") is not None:
            lines.append(f"- Active task progress: {snapshot['progress']}%")
        if snapshot.get("deadline_date"):
            lines.append(
                f"- Active task deadline: {snapshot['deadline_date']}"
            )

    if pending_instruction:
        lines.append(f"- Pending instruction: {pending_instruction}")

    if history:
        lines.append("Recent conversation:")

        for turn in history:
            role = str(turn.get("role") or "user").capitalize()
            text = str(turn.get("text") or "").strip()
            at = str(turn.get("at") or "").strip()

            if text:
                stamp = f" [{at}]" if at else ""
                lines.append(f"{role}{stamp}: {text}")

    return "\n".join(lines)


def _resolve_client(client=None):
    """The model client: the one of the project, or an injected one."""

    if client is not None:
        return client

    from ai.text_extractor import client as cohere_client

    return cohere_client


def _model_name() -> str:
    from ai.text_extractor import MODEL_NAME

    return MODEL_NAME


def _parse_answer(text: str) -> dict | None:
    """Read the JSON object of the model answer.

    The answer is a JSON object. When the model writes its reasoning before
    the answer, the last JSON object of the text is the answer.
    """

    from ai.text_extractor import parse_llm_json_candidates

    for candidate in parse_llm_json_candidates(text):
        if "rewritten" in candidate or "needs_clarification" in candidate:
            return candidate

    return None


def _call_model(prompt: str, client=None) -> dict | None:
    """Ask the model for a rewrite. ``None`` when the model does not answer."""

    response = _resolve_client(client).chat(
        model=_model_name(),
        messages=[
            {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    text_segments = [
        item
        for item in response.message.content
        if getattr(item, "type", None) == "text"
    ]

    if not text_segments:
        return None

    # The last text segment is the answer of the model.
    return _parse_answer(text_segments[-1].text)


def _validate(
    original: str,
    rewritten: str,
    *,
    allowed_task_ids: set[int],
    active_task_id: int | None,
    pending_values: set[str] | None = None,
) -> str | None:
    """Check the rewrite. ``None`` when it may not be used."""

    if not isinstance(rewritten, str):
        return None

    candidate = rewritten.strip()

    if not candidate:
        return None

    # Every value the employee stated has to survive the rewrite.
    if not _numbers(original).issubset(_numbers(candidate)):
        return None

    # The rewrite may only mention a task id that is already known.
    if not _task_ids(candidate).issubset(allowed_task_ids):
        return None

    if pending_values and is_task_reference_only(original):
        # The message only named a task: the rewrite has to carry the task id
        # of the message and the values of the change that was asked for.
        if not _task_ids(original).issubset(_task_ids(candidate)):
            return None

        if not pending_values.issubset(_numbers(candidate)):
            return None

        return candidate

    # The message did not name a task itself, so the rewrite has to name the
    # task of the memory or one of the displayed candidates. Without it the
    # message stays as vague as it was.
    known_ids = _task_ids(candidate)

    if active_task_id is not None and active_task_id not in known_ids:
        other_known_ids = allowed_task_ids - _task_ids(original)

        if not (known_ids & other_known_ids):
            return None

    return candidate


# --- the service -----------------------------------------------------------------


def rewrite_query(
    user_input: str,
    *,
    memory: dict | None = None,
    history: list | None = None,
    client=None,
) -> str:
    """Turn a follow-up message into a standalone instruction.

    The original message is returned when it is already standalone, when the
    context is not enough, or when the model fails. The rewrite never
    contains a value that the employee did not state, and never a task id
    that is not known.
    """

    rewritten, _status = rewrite_query_with_status(
        user_input, memory=memory, history=history, client=client
    )

    return rewritten


def rewrite_query_with_status(
    user_input: str,
    *,
    memory: dict | None = None,
    history: list | None = None,
    client=None,
) -> tuple[str, str]:
    """Rewrite a follow-up and report how the rewrite ended.

    The status is ``not_needed``, ``success``, ``failed``, ``clarification``,
    ``rejected`` or ``rejected_task_id``. The last one means the model named a
    task id that is not part of the conversation; callers must not silently
    redirect such an instruction to another task.
    """

    original = (user_input or "").strip()

    if not original:
        return original, "not_needed"

    active_task_id, snapshot = _active_task(memory)

    pending_instruction = _pending_instruction(memory)

    if not needs_rewrite(
        original,
        active_task_id=active_task_id,
        pending_instruction=pending_instruction,
    ):
        return original, "not_needed"

    turns = _history_of(memory, history)

    prompt = "\n\n".join(
        [
            _context_lines(
                active_task_id,
                snapshot,
                turns,
                pending_instruction,
                last_action=(memory or {}).get("last_action"),
                candidates=_candidates_of(memory),
                clarification=_clarification_of(memory),
            ),
            f'Message to rewrite: "{original}"',
        ]
    )

    try:
        answer = _call_model(prompt, client=client)
    except Exception as error:  # noqa: BLE001 - the original message is used
        logger.warning("Query rewriting failed: %s", error)
        return original, "failed"

    if not isinstance(answer, dict):
        return original, "failed"

    if answer.get("needs_clarification"):
        return original, "clarification"

    allowed_task_ids = (
        _task_ids(original)
        | {active_task_id}
        | {
            candidate["id"]
            for candidate in _candidates_of(memory)
            if isinstance(candidate.get("id"), int)
        }
    )

    rewritten = answer.get("rewritten")

    if isinstance(rewritten, str):
        rewritten_ids = _task_ids(rewritten)

        if rewritten_ids and not rewritten_ids.issubset(allowed_task_ids):
            logger.info(
                "The rewrite named an unknown task, the original message is used."
            )
            return original, "rejected_task_id"

    candidate = _validate(
        original,
        rewritten,
        allowed_task_ids=allowed_task_ids,
        active_task_id=active_task_id,
        pending_values=_pending_values(memory),
    )

    if candidate is None:
        logger.info("The rewrite was rejected, the original message is used.")
        return original, "rejected"

    return candidate, "success"
