"""A small, bounded tool-selection loop for read requests.

The agent does not execute anything itself. It selects one of the task tools,
inspects the returned observation, and either continues with another tool or
returns the answer. The loop is bounded and identical calls are rejected, so a
request can never run forever.

Writes (create/update/delete) stay in the existing service flow, which owns the
confirmation and validation rules. This agent is the retrieval capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from database import task_fields
from services import task_tools

MAX_AGENT_STEPS = 4


def _task_id(task) -> int | None:
    if isinstance(task, dict):
        return task.get("id")

    return getattr(task, "id", None)


@dataclass
class ReadRequest:
    message: str
    task_id: int | None = None
    context_task_id: int | None = None
    employee: str | None = None
    requested_field: str | None = None
    use_context: bool = False
    user_id: str | None = None
    conversation_id: str | None = None
    search_fn: Callable[[], list] | None = None
    context_fn: Callable[[], dict] | None = None
    task_fn: Callable | None = None
    field_fn: Callable | None = None


@dataclass
class AgentState:
    target: int | None = None
    matches: list | None = None
    search_done: bool = False
    context_done: bool = False
    calls: list = field(default_factory=list)


class TaskAgent:
    """Select tools until the requested task information is available."""

    def __init__(self, max_steps: int = MAX_AGENT_STEPS):
        self.max_steps = max_steps

    def run(self, request: ReadRequest) -> dict:
        initial_target = request.task_id

        if initial_target is None and not request.use_context:
            initial_target = request.context_task_id

        state = AgentState(target=initial_target)
        seen: set = set()

        for _ in range(self.max_steps):
            action = self._select(state, request)

            if action is None:
                break

            signature = (
                action["tool"],
                action.get("task_id"),
                action.get("field"),
            )

            if signature in seen:
                break

            seen.add(signature)
            state.calls.append(action)

            result = self._execute(action, request, state)

            if result is not None:
                return result

        if state.matches is not None and not state.matches:
            return {"status": "not_found", "task_id": state.target}

        if state.target is not None:
            return {"status": "error", "message": "The task could not be read."}

        return {
            "status": "clarification_required",
            "message": "Which task are you referring to? Please provide its task ID.",
        }

    def _select(self, state: AgentState, request: ReadRequest) -> dict | None:
        if state.target is None:
            if request.use_context and not state.context_done:
                return {"tool": "get_conversation_context"}

            if request.search_fn is not None and not state.search_done:
                return {"tool": "search_tasks"}

            return None

        if request.requested_field:
            return {
                "tool": "get_task_field",
                "task_id": state.target,
                "field": request.requested_field,
            }

        return {"tool": "get_task", "task_id": state.target}

    def _execute(self, action: dict, request: ReadRequest, state: AgentState):
        name = action["tool"]

        if name == "get_conversation_context":
            state.context_done = True
            result = self._context_result(request)
            active = result.get("active_task_id")

            if active is not None:
                state.target = active

            return None

        if name == "search_tasks":
            state.search_done = True
            matches = request.search_fn() if request.search_fn else []
            state.matches = list(matches or [])

            if len(state.matches) == 1:
                state.target = _task_id(state.matches[0])
                return None

            if len(state.matches) > 1:
                return {
                    "status": "task_selection_required",
                    "message": "I found more than one matching task. Which one do you mean?",
                    "tasks": state.matches,
                }

            return None

        if name == "get_task_field":
            if request.field_fn is not None:
                result = request.field_fn(
                    state.target,
                    action.get("field"),
                    employee=request.employee,
                )
            else:
                result = task_tools.get_task_field(
                    state.target, action.get("field"), employee=request.employee
                )

            if result.get("status") == "not_found":
                return {"status": "not_found", "task_id": state.target}

            return result

        if name == "get_task":
            if request.task_fn is not None:
                task = request.task_fn(state.target, employee=request.employee)
            else:
                task = task_tools.get_task(state.target, employee=request.employee)

            if task is None:
                return {"status": "not_found", "task_id": state.target}

            return {
                "status": "success",
                "task_id": _task_id(task),
                "task": task,
                "requested_field": request.requested_field,
            }

        return None

    @staticmethod
    def _context_result(request: ReadRequest) -> dict:
        if request.context_fn is not None:
            return request.context_fn() or {}

        if request.user_id and request.conversation_id:
            return task_tools.get_conversation_context(
                request.user_id, request.conversation_id
            )

        return {}
