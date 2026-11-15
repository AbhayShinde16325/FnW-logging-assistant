"""Explicit intent detection for employee messages.

The conversational engine used to treat *every* new message as a task
description, which meant that "update my business report task progress to 70%"
started a brand new task instead of changing the existing one.

This module classifies a message into exactly one intent before any task
extraction happens:

- ``CREATE_TASK``          -> build a new task
- ``UPDATE_TASK``          -> change fields on an existing task
- ``GET_TASK``             -> read one existing task
- ``LIST_TASKS``           -> read several existing tasks
- ``DELETE_TASK``          -> remove an existing task (explicit confirmation)
- ``GENERAL_QUERY``        -> ordinary conversation, no database work
- ``CLARIFICATION_REQUIRED`` -> an intent is known but a detail is missing

The rules are deterministic and provider independent: no LLM call is made to
decide *what* the employee wants, so the decision cannot drift or invent
values. The extraction model is only used afterwards, for the fields
themselves.

Two rules make the classification unambiguous:

1. An *explicit* creation request ("add a task", "create a task") is always a
   ``CREATE_TASK``. It is never a lookup of an existing task, whatever the
   wording of the task description is.
2. An update request that refers to "its" / "it" only makes sense when the
   conversation already identified a task, so ``detect_intent`` accepts the
   conversation context (``task_in_context``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CREATE_TASK = "CREATE_TASK"
UPDATE_TASK = "UPDATE_TASK"
GET_TASK = "GET_TASK"
LIST_TASKS = "LIST_TASKS"
DELETE_TASK = "DELETE_TASK"
GENERAL_QUERY = "GENERAL_QUERY"
CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"

INTENTS = (
    CREATE_TASK,
    UPDATE_TASK,
    GET_TASK,
    LIST_TASKS,
    DELETE_TASK,
    GENERAL_QUERY,
    CLARIFICATION_REQUIRED,
)

# Intents that may read from or change the database.
DATABASE_INTENTS = frozenset(
    {CREATE_TASK, UPDATE_TASK, GET_TASK, LIST_TASKS, DELETE_TASK}
)


@dataclass(frozen=True)
class Intent:
    """The detected intent plus a short, human-readable reason."""

    name: str
    reason: str
    confidence: float = 1.0

    @property
    def is_database_operation(self) -> bool:
        return self.name in DATABASE_INTENTS

    def as_dict(self) -> dict:
        return {
            "intent": self.name,
            "reason": self.reason,
            "confidence": self.confidence,
        }


# --- keyword sets ---------------------------------------------------------------

# Unambiguous instructions to remove a task. Checked first so that "delete"
# is never mistaken for "change".
DELETE_PATTERN = re.compile(
    r"\b(delete|remove|erase|drop|discard)\b|\bget rid of\b|\bcancel (the|my) task\b",
    re.IGNORECASE,
)

# Unambiguous instructions to change an existing task.
UPDATE_PATTERN = re.compile(
    r"\b(update|modify|change|revise|rename|reassign|reschedule|postpone)\b"
    r"|\b(increase|decrease|reduce|bump|raise)\b[^.]{0,20}\bprogress\b"
    r"|\bmark\b[^.]{0,30}\b(as|it|complete|completed|done|finished|in progress)\b"
    r"|\b(moving|move|push|advance)\b[^.]{0,20}\b(deadline|due date)\b"
    r"|\bcorrect (the|my)\b",
    re.IGNORECASE,
)

# A statement that the work itself is finished.
COMPLETION_PATTERN = re.compile(
    r"\b(is|are|was|were)\s+(done|finished|completed)\b"
    r"|\b(done|finished|completed)\s+(with|it)\b"
    r"|\b(have|has|had)\s+(completed|finished)\b"
    r"|\bmark (it|the task)\b",
    re.IGNORECASE,
)

# An explicit "build a new task" command at the start of the message. It wins
# over the update wording, so "create a task to update the documentation" is
# still a creation.
STRONG_CREATE_PATTERN = re.compile(
    r"^\s*(create|add|make|schedule|set up|plan|new)\b", re.IGNORECASE
)

# An explicit instruction to build a new task, anywhere in the message.
# "Add a task for RAM" and "Create a task to prepare the report" are both
# creations, and neither of them may be answered with a lookup of an
# existing task.
EXPLICIT_CREATE_PATTERN = re.compile(
    r"\b(create|add|register|log|record|put|insert|schedule|plan|make|set up)"
    r"\s+(me\s+)?(a|an|the|one|another|new|my|this)?\s*(new\s+)?tasks?\b",
    re.IGNORECASE,
)

# An explicit instruction to change a task that already exists. It is used to
# leave a running creation conversation: "update my existing task" is never
# the title of a new task.
EXISTING_TASK_UPDATE_PATTERN = re.compile(
    r"\b(update|change|modify|rename|revise|reschedule|reassign|edit|amend)\b"
    r"[^.]{0,40}\b(existing|previous|old|current|earlier|same)\b[^.]{0,20}\btasks?\b"
    r"|\b(update|change|modify|rename|revise|reschedule|reassign|edit|amend)\b"
    r"[^.]{0,20}\btasks?\s*#?\s*\d+"
    r"|\b(update|change|modify|rename|revise|reschedule|reassign|edit|amend)\b"
    r"[^.]{0,20}\bmy\b[^.]{0,30}\btasks?\b"
    r"|\b(its|their)\s+(progress|status|deadline|title|date|time|assignee|reminder)\b",
    re.IGNORECASE,
)

# An update request that refers to a task by a pronoun. It only has a
# meaning when the conversation already identified the task, so it needs the
# context before it can be classified.
CONTEXT_ACTION_PATTERN = re.compile(
    r"\b(change|update|modify|rename|revise|reschedule|reassign|edit|amend|"
    r"set|mark|bump|increase|decrease|reduce|raise)\s+(its|it|that|this)\b"
    r"|\b(its|their)\s+(progress|status|deadline|title|date|time|assignee|reminder)\b",
    re.IGNORECASE,
)

# Weak update wording: only an update when no creation verb is present.
WEAK_UPDATE_PATTERN = re.compile(
    r"\bset\b[^.]{0,30}\b(progress|status|deadline|title|assignee|date|time)\b"
    r"|\b(progress|status|deadline|reminder|title)\b[^.]{0,20}\bto\b[^.]{0,20}\d",
    re.IGNORECASE,
)

# Explicit instructions to build a new task.
CREATE_PATTERN = re.compile(
    r"\b(create|schedule|plan)\b"
    r"|\badd (a|an|new|another|one more)\b"
    r"|\bnew task\b"
    r"|\bmake (a|an)\b[^.]{0,20}\btask\b"
    r"|\bset up\b"
    r"|\b(i|we) (need|have) to\b"
    r"|\b(i|we) must\b"
    r"|\bassign (a|an|new)\b"
    r"|\bremind me to\b"
    r"|\bnote down\b",
    re.IGNORECASE,
)

# Requests that span several tasks.
LIST_PATTERN = re.compile(
    r"\blist\b|\bshow (me )?(all|every)\b|\b(all|every) (my|the)\b"
    r"|\bshow\b[^.]{0,30}\btasks\b|\bget\b[^.]{0,30}\btasks\b"
    r"|\b(all|every|any)\b[^.]{0,30}\btasks?\b"
    r"|\b(what|which|how many)\b[^.]{0,25}\btasks?\b"
    r"|^\s*(?:the\s+)?tasks?\b[^.]{0,30}\b(pending|open|in progress|"
    r"completed|overdue|due|assigned)\b"
    r"|\bmy tasks\b|\bpending tasks\b|\bin progress tasks\b"
    r"|\bcompleted tasks\b|\boverdue tasks\b|\bupcoming tasks\b"
    r"|\bsummar(y|ise|ize)\b|\boverview\b",
    re.IGNORECASE,
)

# Questions / requests about one task. "Retrieve", "fetch" and "get" are
# included because a rewritten follow-up is often phrased that way
# ("Retrieve the assigned date of task ID 211"). "Get me a new task" is still
# a creation, so "get" is ignored when it asks for a/an/new task.
GET_PATTERN = re.compile(
    r"\b(what('s| is| are)|which|how (much|far)|tell me|show me|check|"
    r"give me|status of|details of|info about|information about|read|"
    r"retrieve|fetch)\b"
    r"|\bget\b(?!\s+(?:me\s+)?(?:a|an|new|another)\b)",
    re.IGNORECASE,
)

TASK_WORD_PATTERN = re.compile(r"\btasks?\b", re.IGNORECASE)
PLURAL_TASK_PATTERN = re.compile(r"\btasks\b", re.IGNORECASE)

# A question about one property of a task, even when the message does not say
# the word "task" ("What is its current deadline date?"). The property words
# are semantic; the service maps them to validated columns. The patterns are
# anchored to a question or a short reference, so a declarative task
# description ("its progress is 80%") is never mistaken for a read.
_FIELD_WORDS = (
    r"progress|completion|percentage|percent|status|state|deadline|due date|"
    r"due|reminder|remind(?:er|ed|ing)?|assignee|assigned(?:\s+to|\s+date|"
    r"\s+time)?|title|owner|responsible|done|completed|complete|finished"
)

FIELD_QUESTION_PATTERN = re.compile(
    rf"^\s*(?:what(?:'s|\s+is|\s+are)?|how\s+much|how\s+many|when|who|"
    rf"which|tell\s+me|show\s+me|is|are|has|have)\b[^?.!]{{0,100}}"
    rf"\b(?:{_FIELD_WORDS})\b"
    rf"|^\s*(?:and|what\s+about|how\s+about)\b[^?.!]{{0,60}}"
    rf"\b(?:{_FIELD_WORDS})\b"
    rf"|^\s*(?:its|it|this|that|the\s+same)\b\s+(?:current\s+|exact\s+|"
    rf"latest\s+|actual\s+)?(?:{_FIELD_WORDS})\b\s*(?:\?+\s*|$)"
    rf"|^\s*(?:has|have)\s+(?:it|this|that)\s+been\s+"
    rf"(?:completed|done|finished)\b",
    re.IGNORECASE,
)

# A declarative "this is a new task" opening. It is a creation even when the
# employee does not start with "create" or "add". An update/deletion wording
# still wins, so "update my new task" is not a creation.
NEW_TASK_DECLARATION_PATTERN = re.compile(
    r"^\s*(?:(?:at|for|about|here'?s|this\s+is|it'?s)\s+)?"
    r"(?:a|an|the|my)?\s*(?:new|another|one\s+more)\s+tasks?\b",
    re.IGNORECASE,
)

QUESTION_START_PATTERN = re.compile(
    r"^\s*(what|whats|which|who|whom|whose|when|where|why|how|is|are|"
    r"do|does|did|can|could|should|would|will|tell|show|list|give)\b",
    re.IGNORECASE,
)

GENERAL_PATTERN = re.compile(
    r"^\s*(hello|hi|hey|yo|thanks|thank you|ok|okay|sure|bye|goodbye|"
    r"good (morning|afternoon|evening)|who are you|what can you do|"
    r"how do you work|what are you)\b",
    re.IGNORECASE,
)

# A message that only names an existing task: "task #211", "#211",
# "task 211". It is the answer to "which task do you mean?", never the
# description of a new task.
TASK_REFERENCE_ONLY_PATTERN = re.compile(
    r"^(?:the\s+|this\s+|that\s+)?(?:task\s*)?#?\s*\d+\s*\.?$",
    re.IGNORECASE,
)

# A search / lookup request about existing tasks. "Do I have a task like
# research about MCP?" and "find my MCP research task" are lookups, not new
# tasks.
SEARCH_PATTERN = re.compile(
    r"\b(find|search|look for|look up|locate|do i have|do we have|"
    r"is there|are there)\b",
    re.IGNORECASE,
)

# A topical phrase that clearly names existing work. When it is combined
# with "about"/"regarding"/"related to", the employee is describing a search
# ("research about MCP"), not dictating a new task.
TOPIC_NOUN_PATTERN = re.compile(
    r"\b(research|report|review|analysis|meeting|call|design|document|"
    r"documentation|docs|api|database|db|mcp|server|servers|bug|issue|"
    r"feature|project|test|testing|deploy|deployment|migration|onboarding|"
    r"invoice|budget|proposal|presentation|interview|training|marketing|"
    r"sales|backend|frontend|client)\b",
    re.IGNORECASE,
)

RELATION_PATTERN = re.compile(
    r"\b(about|regarding|related to|on|for)\b", re.IGNORECASE
)

# A short noun phrase that ends in "task(s)" ("MCP servers task"). It is a
# lookup unless it also contains an action that describes a new task.
TASK_PHRASE_END_PATTERN = re.compile(r"\btasks?\s*[?.!]*$", re.IGNORECASE)
TASK_PHRASE_ACTION_PATTERN = re.compile(
    r"\b(has|have|had|want|wants|need|needs|create|add|make|schedule|"
    r"assign|assigned|remind|note|plan|set|update|change|delete|remove|"
    r"rename|complete|finish|finished|done|start|begin|working|work on)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern
    intent: str
    reason: str


def _normalize(user_input: str) -> str:
    """The comparable form of a message: lower case, single spaced."""

    return " ".join((user_input or "").strip().lower().replace(",", " ").split())


def is_task_reference_only(user_input: str) -> bool:
    """True when the message only names an existing task.

    "Task #211" selects a task of the conversation. It is never the
    description of a new task, so it never starts a creation.
    """

    return bool(TASK_REFERENCE_ONLY_PATTERN.match(_normalize(user_input)))


def is_search_request(user_input: str) -> bool:
    """True when the message looks for existing tasks.

    A search never creates or changes a task. The rules are deliberately
    explicit: a search verb ("find", "do I have"), a topic with "about", or
    a short noun phrase that ends in "task". An action verb keeps the message
    a creation (or an update), so "add a task about MCP" is still a creation.
    """

    text = _normalize(user_input)

    if not text or DELETE_PATTERN.search(text):
        return False

    if is_explicit_create_request(text) or UPDATE_PATTERN.search(text):
        return False

    if SEARCH_PATTERN.search(text):
        return True

    if TOPIC_NOUN_PATTERN.search(text) and RELATION_PATTERN.search(text):
        return True

    if (
        len(text.split()) <= 8
        and TASK_PHRASE_END_PATTERN.search(text)
        and not TASK_PHRASE_ACTION_PATTERN.search(text)
    ):
        # "some task" or "new task" carry no topic, so they are not a
        # search; they keep their old meaning (a plain task description).
        filler = {
            "task", "tasks", "some", "any", "a", "an", "the", "my", "new",
            "this", "that", "these", "those",
        }
        topics = [
            word
            for word in re.findall(r"[a-z0-9]+", text)
            if word not in filler
        ]

        if topics:
            return True

    return False


def is_explicit_create_request(user_input: str) -> bool:
    """True when the employee explicitly asks for a *new* task.

    "Add a task" and "Create a task" are creations, never a lookup of an
    existing task. A message that asks for a deletion is not a creation.
    """

    text = _normalize(user_input)

    if not text or DELETE_PATTERN.search(text):
        return False

    if EXPLICIT_CREATE_PATTERN.search(text):
        return True

    if STRONG_CREATE_PATTERN.match(text) and TASK_WORD_PATTERN.search(text):
        return True

    if NEW_TASK_DECLARATION_PATTERN.match(text):
        return True

    return False


def is_explicit_update_request(user_input: str) -> bool:
    """True when the employee explicitly asks to change an existing task."""

    text = _normalize(user_input)

    if not text or DELETE_PATTERN.search(text):
        return False

    return bool(EXISTING_TASK_UPDATE_PATTERN.search(text))


def needs_task_context(user_input: str) -> bool:
    """True when the message only makes sense with an identified task.

    "Change its progress" points at a task the conversation already knows,
    so the classifier needs to know whether such a task exists.
    """

    return bool(CONTEXT_ACTION_PATTERN.search(_normalize(user_input)))


def detect_intent(user_input: str, *, task_in_context: bool | None = None) -> Intent:
    """Classify one employee message into a single intent.

    ``task_in_context`` tells the classifier whether the conversation
    already identified a task:

    * ``True``  - a task is known, so "change its progress" is an update,
    * ``False`` - no task is known, so the employee has to be asked which
      task is meant,
    * ``None``  - unknown (the default), the message is classified on its
      own wording.
    """

    raw = (user_input or "").strip()
    text = _normalize(raw)

    if not text:
        return Intent(
            CLARIFICATION_REQUIRED,
            "The message is empty, so the intent is unknown.",
            confidence=0.0,
        )

    # 1. DELETE - destructive, checked first so it never reads as "change".
    if DELETE_PATTERN.search(text):
        return Intent(DELETE_TASK, "The message asks to remove a task.")

    # 2. CREATE - an explicit "build a new task" command always wins, even
    #    when the task description itself contains words like "update".
    #    A creation never looks an existing task up.
    if is_explicit_create_request(text):
        return Intent(CREATE_TASK, "The message asks for a new task.")

    # 3. UPDATE - an explicit instruction to change an existing task.
    if UPDATE_PATTERN.search(text):
        if (
            task_in_context is False
            and needs_task_context(text)
        ):
            return Intent(
                CLARIFICATION_REQUIRED,
                "The message changes a task that has not been identified yet.",
            )

        return Intent(UPDATE_TASK, "The message asks to change an existing task.")

    # 3b. LIST - a question about several tasks ("which tasks are in
    #     progress") is a list even though it names a task property.
    if LIST_PATTERN.search(text) and PLURAL_TASK_PATTERN.search(text):
        return Intent(LIST_TASKS, "The message asks for several tasks.")

    # 3c. GET - a question about one task property. It is a read even when
    #     the message does not repeat the word "task".
    if FIELD_QUESTION_PATTERN.search(text):
        if task_in_context is False:
            return Intent(
                CLARIFICATION_REQUIRED,
                "The message asks about a task that has not been identified yet.",
            )

        return Intent(GET_TASK, "The message asks about one task property.")

    # 3. LIST - a request spanning several tasks.
    if LIST_PATTERN.search(text) and (
        PLURAL_TASK_PATTERN.search(text) or "list" in text
    ):
        return Intent(LIST_TASKS, "The message asks for several tasks.")

    # 4. GET - a question or request about one specific task.
    if GET_PATTERN.search(text) and TASK_WORD_PATTERN.search(text):
        return Intent(GET_TASK, "The message asks about one existing task.")

    if QUESTION_START_PATTERN.match(text) and TASK_WORD_PATTERN.search(text):
        return Intent(
            GET_TASK, "The message is a question about an existing task."
        )

    # 5. SEARCH - a lookup of existing work ("research about MCP",
    #    "MCP servers task", "find my MCP research task"). It reads, it never
    #    creates or changes anything.
    if is_search_request(text):
        if PLURAL_TASK_PATTERN.search(text):
            return Intent(
                LIST_TASKS, "The message searches several existing tasks."
            )

        return Intent(GET_TASK, "The message searches existing tasks.")

    # 6. CREATE - an explicit creation instruction.
    if CREATE_PATTERN.search(text):
        return Intent(CREATE_TASK, "The message asks for a new task.")

    # 7. UPDATE - "the work is done" or a bare "set the status to ...".
    if COMPLETION_PATTERN.search(text) or WEAK_UPDATE_PATTERN.search(text):
        if (
            task_in_context is False
            and needs_task_context(text)
        ):
            return Intent(
                CLARIFICATION_REQUIRED,
                "The message changes a task that has not been identified yet.",
            )

        return Intent(UPDATE_TASK, "The message asks to change an existing task.")

    # 8. A message that only names an existing task ("Task #211") selects
    #    that task. It is the answer to "which task do you mean?", never the
    #    description of a new task.
    if is_task_reference_only(text):
        return Intent(
            UPDATE_TASK,
            "The message names an existing task, so it is not a new task.",
        )

    # 9. CREATE - a plain task description ("Rahul has a research task").
    if TASK_WORD_PATTERN.search(text) and not QUESTION_START_PATTERN.match(text):
        return Intent(CREATE_TASK, "The message describes a new task.")

    # 10. GENERAL - greetings, capability questions, anything else.
    if GENERAL_PATTERN.search(text):
        return Intent(GENERAL_QUERY, "The message is ordinary conversation.")

    return Intent(
        GENERAL_QUERY,
        "The message is not a recognised task operation.",
        confidence=0.5,
    )


def is_update_request(user_input: str, *, task_in_context: bool | None = None) -> bool:
    """Convenience helper used by the service layer."""

    return (
        detect_intent(user_input, task_in_context=task_in_context).name
        == UPDATE_TASK
    )


def is_create_request(user_input: str) -> bool:
    """Convenience helper used by the service layer."""

    return detect_intent(user_input).name == CREATE_TASK
