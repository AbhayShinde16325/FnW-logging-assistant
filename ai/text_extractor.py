import os
import json
import re
from time import sleep

import cohere
from cohere import TooManyRequestsError
from cohere.core import ApiError
from dotenv import load_dotenv

load_dotenv()

from datetime import date, time
from pydantic import BaseModel, Field, ValidationError, field_validator


MODEL_NAME = "command-a-plus-05-2026"
MAX_ATTEMPTS = 2

client = cohere.ClientV2(api_key=os.environ["COHERE_API_KEY"])


# --- structured output --------------------------------------------------------------

# The keywords of a JSON schema that the model endpoint accepts. Everything
# else (the numeric bounds, the string lengths, the formats, ...) is enforced
# by the Pydantic model, not by the schema.
SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "items",
        "$defs",
        "$ref",
        "anyOf",
        "oneOf",
        "allOf",
        "enum",
        "const",
        "required",
        "additionalProperties",
        "description",
        "title",
        "default",
    }
)


def _clean_schema(node):
    """Keep only the keywords the model endpoint understands."""

    if isinstance(node, dict):
        cleaned = {}

        for key, value in node.items():
            if key not in SCHEMA_KEYWORDS:
                continue

            if key in ("properties", "$defs"):
                # The keys of these objects are names, not keywords.
                cleaned[key] = {
                    name: _clean_schema(sub) for name, sub in value.items()
                }
            else:
                cleaned[key] = _clean_schema(value)

        return cleaned

    if isinstance(node, list):
        return [_clean_schema(value) for value in node]

    return node


def _require_properties(schema: dict) -> dict:
    """Every property of an object is required by the model endpoint."""

    if schema.get("type") == "object" and "properties" in schema:
        schema["required"] = list(schema["properties"])

    for definition in (schema.get("$defs") or {}).values():
        _require_properties(definition)

    return schema


def json_schema_for(model) -> dict:
    """The JSON schema of a Pydantic model, in the shape the API accepts.

    The schema is the structured output of the model: it cannot answer with a
    value of the wrong type any more, so a task id is a number (or null) and
    never an unquoted word.
    """

    schema = _clean_schema(model.model_json_schema())

    return _require_properties(schema)


def response_format_for(model) -> dict:
    """The structured output of the model for one Pydantic model."""

    return {"type": "json_object", "json_schema": json_schema_for(model)}


def ask_model(system_prompt: str, user_input: str, model) -> str:
    """Ask the model for a structured answer and return its text.

    The schema of the Pydantic model is the structured output of the model,
    so the answer has the right types: a task id is a number or null and
    never an unquoted word. When the endpoint does not accept the schema, a
    plain JSON object is asked for and the Pydantic model validates it.
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]

    try:
        response = client.chat(
            model=MODEL_NAME,
            messages=messages,
            response_format=response_format_for(model),
            thinking={"type": "disabled"},
            temperature=0,
        )
    except ApiError as error:
        if "json_schema" not in str(error):
            raise

        response = client.chat(
            model=MODEL_NAME,
            messages=messages,
            response_format={"type": "json_object"},
            thinking={"type": "disabled"},
            temperature=0,
        )

    text_segments = [
        item
        for item in response.message.content
        if getattr(item, "type", None) == "text"
    ]

    if not text_segments:
        raise IndexError("The model returned no text content.")

    # The last text segment is the answer of the model.
    return text_segments[-1].text



class TaskDetails(BaseModel):
    task_title: str | None = Field(default=None, min_length=1)
    assigned_to: str | None = None
    assigned_date: date | None = None
    assigned_time: time | None = None
    deadline_date: date | None = None
    deadline_time: time | None = None
    status: str = "PENDING"
    progress: int = Field(default=0, ge=0, le=100)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        allowed_statuses = {
            "PENDING",
            "IN_PROGRESS",
            "COMPLETED"
        }

        value = value.upper()

        if value not in allowed_statuses:
            raise ValueError("Invalid task status.")

        return value


class ExtractionResult(BaseModel):
    """Raw extraction used by the interactive draft flow.

    Date/time values are returned VERBATIM as phrases; the deterministic
    `datetime_resolver` converts them later. Never stored directly.
    """

    task_title: str | None = None
    assigned_to: str | None = None
    assigned_when: str | None = None
    deadline_when: str | None = None
    reminder_when: str | None = None
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is None:
            return None

        allowed_statuses = {
            "PENDING",
            "IN_PROGRESS",
            "COMPLETED"
        }

        value = value.upper()

        if value not in allowed_statuses:
            raise ValueError("Invalid task status.")

        return value


class TaskRecord(TaskDetails):
    """Final, fully-confirmed task: the 8 stored fields + the reminder.

    reminder_date / reminder_time map to the reminder columns on the Task
    model and are persisted by the repository.
    """

    reminder_date: date | None = None
    reminder_time: time | None = None


class TaskUpdateFind(BaseModel):
    """Criteria used to locate the existing task that should be changed."""

    task_id: int | None = None
    task_title: str | None = None
    assigned_to: str | None = None


class TaskUpdateChanges(BaseModel):
    """Only the fields the user explicitly asked to change."""

    task_title: str | None = None
    assigned_to: str | None = None
    assigned_when: str | None = None
    deadline_when: str | None = None
    reminder_when: str | None = None
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is None:
            return None

        value = value.upper()

        if value not in {"PENDING", "IN_PROGRESS", "COMPLETED"}:
            raise ValueError("Invalid task status.")

        return value


class TaskUpdate(BaseModel):
    """Structured update request: which task to find and what to change."""

    find: TaskUpdateFind = Field(default_factory=TaskUpdateFind)
    changes: TaskUpdateChanges = Field(default_factory=TaskUpdateChanges)

    @field_validator("find", "changes", mode="before")
    @classmethod
    def _empty_when_null(cls, value):
        """The model sometimes answers with null for a nested object."""

        return {} if value is None else value


class TaskQueryFilters(BaseModel):
    """Filters applied when reading one or more tasks."""

    status: str | None = None
    assigned_to: str | None = None
    title_contains: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is None:
            return None

        value = value.upper()

        if value not in {"PENDING", "IN_PROGRESS", "COMPLETED"}:
            raise ValueError("Invalid task status.")

        return value


class TaskQuery(BaseModel):
    """Structured read request: which task to find and how to filter.

    ``requested_field`` is a semantic property identifier, not a SQL column.
    The service validates it against ``database.task_fields`` before use.
    """

    find: TaskUpdateFind = Field(default_factory=TaskUpdateFind)
    filters: TaskQueryFilters = Field(default_factory=TaskQueryFilters)
    requested_field: str | None = None

    @field_validator("find", "filters", mode="before")
    @classmethod
    def _empty_when_null(cls, value):
        """The model sometimes answers with null for a nested object."""

        return {} if value is None else value


def sync_status_progress(
    status,
    progress,
    *,
    status_changed: bool = True,
    progress_changed: bool = True,
):
    """Return a status/progress pair that satisfies the business rules.

    Status and progress are kept in sync instead of rejecting a reply:
      - progress above zero moves PENDING to IN_PROGRESS;
      - COMPLETED forces 100%, unless the user only changed progress, in
        which case the status is downgraded to match the new progress.
    """
    if status is None or progress is None:
        return status, progress

    progress = max(0, min(100, int(progress)))

    if status == "PENDING" and progress > 0:
        return "IN_PROGRESS", progress

    if status == "COMPLETED" and progress != 100:
        if progress_changed and not status_changed:
            return ("IN_PROGRESS" if progress > 0 else "PENDING"), progress
        return "COMPLETED", 100

    return status, progress


def validate_business_rules(task):
    task.status, task.progress = sync_status_progress(task.status, task.progress)

# A value that the model wrote without quotes, for example
# "task_id": 22004d. The JSON is broken, but the value can be handed to the
# Pydantic model as text, so the model decides that it is invalid.
UNQUOTED_VALUE_PATTERN = re.compile(
    r"(:\s*)([A-Za-z0-9_.\-+]+)(\s*(?=[,}\]\n]))"
)

JSON_NUMBER_PATTERN = re.compile(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?")

JSON_KEYWORDS = frozenset({"true", "false", "null"})


def repair_unquoted_values(text):
    """Quote the values the model wrote without quotes.

    A number and the keywords of JSON are valid JSON and stay as they are.
    """

    def replace(match):
        value = match.group(2)

        if value in JSON_KEYWORDS or JSON_NUMBER_PATTERN.fullmatch(value):
            return match.group(0)

        return f'{match.group(1)}"{value}"{match.group(3)}'

    return UNQUOTED_VALUE_PATTERN.sub(replace, text or "")


def parse_llm_json(text):
    """
    Parse the first JSON object in the model's output.

    Tolerates trailing data (a repeated object or extra text) which
    json.loads() rejects with an "Extra data" error.

    A value without quotes ("task_id": 22004d) breaks the JSON. The value is
    quoted and handed to the Pydantic model, which rejects an invalid task id
    on its own. The model is never asked to invent another value.
    """

    text = (text or "").strip()

    try:
        data, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as error:
        repaired = repair_unquoted_values(text)
        start = repaired.find("{")

        if start == -1:
            raise error

        try:
            data, _ = json.JSONDecoder().raw_decode(repaired[start:])
        except json.JSONDecodeError:
            raise error

    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            "The model output is not a JSON object.", text, 0
        )

    return data


def parse_llm_json_candidates(text):
    """Every JSON object of the model output, the last one first.

    The model sometimes writes its reasoning before the answer, so the answer
    is not always the first object of the output. The objects are returned
    from the last one to the first one: the answer comes last.
    """

    text = repair_unquoted_values(text or "").strip()

    decoder = json.JSONDecoder()
    candidates = []
    index = 0

    while True:
        start = text.find("{", index)

        if start == -1:
            break

        try:
            data, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue

        if isinstance(data, dict):
            candidates.append(data)

        index = start + end

    return list(reversed(candidates))


def validate_model_output(model, raw_output):
    """The JSON object of the model output that the Pydantic model accepts.

    The candidates are tried from the last one to the first one, so the
    answer of the model wins over an example it wrote while thinking. A
    candidate with an invalid value (a task id that is not a number) is
    rejected by the Pydantic model, never converted into another value.
    """

    last_error = None

    for candidate in parse_llm_json_candidates(raw_output):
        try:
            return model.model_validate(candidate)
        except ValidationError as error:
            last_error = error
            continue

    if last_error is not None:
        raise last_error

    raise json.JSONDecodeError(
        "The model output is not a JSON object.", raw_output or "", 0
    )


class TaskExtractor:

    def extract_task(self, user_input):
        system_prompt = """
        You are a task information extraction assistant.

        Extract task information from the user's input.

        Return ONLY valid JSON with these fields:
        {
            "task_title": "string",
            "assigned_to": "name or null",
            "assigned_date": "YYYY-MM-DD or null",
            "assigned_time": "HH:MM or null",
            "deadline_date": "YYYY-MM-DD or null",
            "deadline_time": "HH:MM or null",
            "status": "PENDING, IN_PROGRESS, or COMPLETED",
            "progress": 0
        }

        Rules:
        1. Read only values that are explicitly stated in the text.
        2. Never calculate, infer, or assume any value. Do not use today's date.
        3. If a value is missing, return null. Never convert words like "aaj", "kal", "today", or "tomorrow" into a date.
        4. Dates must be in YYYY-MM-DD format.
        5. Times must be in 24-hour HH:MM format.
        6. assigned_to is the name of the person the task is assigned to, or null if not stated.
        7. If progress is missing, use 0 unless completed.
        8. If completed, progress must be 100.
        9. Progress must be between 0 and 100.
        10. If progress is above 0, status should normally be IN_PROGRESS.
        11. Understand English and Hinglish.
        12. Return exactly one JSON object, with no text, Markdown, or extra objects before or after it.
        """

        last_error = None
        raw_output = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw_output = ask_model(system_prompt, user_input, TaskDetails)

                task = validate_model_output(TaskDetails, raw_output)

                validate_business_rules(task)

                return task

            except TooManyRequestsError as error:
                if attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Cohere API error: {error}"
                    ) from error

                print(
                    f"Rate limited by Cohere. Retrying in 60 seconds... "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})"
                )
                sleep(60)

            except ApiError as error:
                raise RuntimeError(
                    f"Cohere API error: {error}"
                ) from error

            except (
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError
            ) as error:
                last_error = error

                if attempt >= MAX_ATTEMPTS:
                    preview = (raw_output or "")[:300]
                    raise RuntimeError(
                        f"Invalid response from the AI model after "
                        f"{MAX_ATTEMPTS} attempts: {error}. "
                        f"Raw model output: {preview!r}"
                    ) from error

                # Otherwise loop once more and retry the same input.

    def extract_draft(self, user_input):
        """
        Like extract_task(), but returns raw date/time phrases (assigned_when,
        deadline_when, reminder_when) instead of concrete values, so the
        deterministic datetime_resolver can interpret them later.
        """
        system_prompt = """
        You are a task information extraction assistant.

        Extract task information from the user's input as RAW references.

        Return ONLY valid JSON with these fields:
        {
            "task_title": "string or null",
            "assigned_to": "name or null",
            "assigned_when": "verbatim date/time phrase about when the task was assigned, or null",
            "deadline_when": "verbatim date/time phrase about the deadline, or null",
            "reminder_when": "verbatim phrase about a reminder (e.g. '30 mins before the deadline'), or null",
            "status": "PENDING, IN_PROGRESS, COMPLETED, or null",
            "progress": "integer or null"
        }

        Rules:
        1. Copy date/time phrases VERBATIM from the input (e.g. "tomorrow at 5", "15 November 2026 at 5 PM"). Never convert them into dates or times.
        2. If a phrase or value is missing, return null.
        3. Do not invent information. Do not use today's date.
        4. assigned_to is the person the task is assigned to, or null.
        5. progress is only the stated percentage, or null.
        6. Understand English and Hinglish.
        7. Return exactly one JSON object, with no text, Markdown, or extra objects before or after it.
        """

        last_error = None
        raw_output = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw_output = ask_model(system_prompt, user_input, ExtractionResult)

                return validate_model_output(ExtractionResult, raw_output)

            except TooManyRequestsError as error:
                if attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Cohere API error: {error}"
                    ) from error

                print(
                    f"Rate limited by Cohere. Retrying in 60 seconds... "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})"
                )
                sleep(60)

            except ApiError as error:
                raise RuntimeError(
                    f"Cohere API error: {error}"
                ) from error

            except (
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError
            ) as error:
                last_error = error

                if attempt >= MAX_ATTEMPTS:
                    preview = (raw_output or "")[:300]
                    raise RuntimeError(
                        f"Invalid response from the AI model after "
                        f"{MAX_ATTEMPTS} attempts: {error}. "
                        f"Raw model output: {preview!r}"
                    ) from error

                # Otherwise loop once more and retry the same input.

    def _extract_model(self, system_prompt: str, user_input: str, model):
        """Call the model and validate its JSON into ``model``."""

        last_error = None
        raw_output = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw_output = ask_model(system_prompt, user_input, model)
                return validate_model_output(model, raw_output)

            except TooManyRequestsError as error:
                if attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Cohere API error: {error}"
                    ) from error

                print(
                    f"Rate limited by Cohere. Retrying in 60 seconds... "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})"
                )
                sleep(60)

            except ApiError as error:
                raise RuntimeError(
                    f"Cohere API error: {error}"
                ) from error

            except (
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError
            ) as error:
                last_error = error

                if attempt >= MAX_ATTEMPTS:
                    preview = (raw_output or "")[:300]
                    raise RuntimeError(
                        f"Invalid response from the AI model after "
                        f"{MAX_ATTEMPTS} attempts: {error}. "
                        f"Raw model output: {preview!r}"
                    ) from error

        raise RuntimeError(
            f"Could not extract an update request: {last_error}"
        )

    def extract_update(self, user_input):
        """Extract which existing task to find and which fields to change."""

        system_prompt = """
        You extract an UPDATE request for an existing task.

        Return ONLY valid JSON with this shape:
        {
            "find": {
                "task_id": "integer or null",
                "task_title": "title used to find the task, or null",
                "assigned_to": "assignee used to find the task, or null"
            },
            "changes": {
                "task_title": "new title, or null",
                "assigned_to": "new assignee, or null",
                "assigned_when": "verbatim date/time phrase, or null",
                "deadline_when": "verbatim date/time phrase, or null",
                "reminder_when": "verbatim date/time phrase, or null",
                "status": "PENDING, IN_PROGRESS, COMPLETED, or null",
                "progress": "integer 0-100, or null"
            }
        }

        Rules:
        1. "find" identifies the existing task to change. Put only search
           criteria here, never the new values.
        2. "changes" contains only fields the user explicitly asks to change.
        3. Do not treat background context such as "the task I added a few
           days back" or "my existing task" as a field change.
        4. Copy date/time phrases VERBATIM (e.g. "16 Nov", "5 PM"). Never
           convert them into dates or times.
        5. If the user says "this task" without a title or ID, leave the find
           criteria null.
        6. Do not invent values. If a field is not being changed, return null.
        7. Understand English and Hinglish.
        8. Return exactly one JSON object, with no text or Markdown.
        """

        return self._extract_model(system_prompt, user_input, TaskUpdate)

    def extract_query(self, user_input):
        """Extract how to read tasks: which one, and how to filter a list."""

        system_prompt = """
        You extract a READ request for existing tasks.

        Return ONLY valid JSON with this shape:
        {
            "find": {
                "task_id": "integer or null",
                "task_title": "title used to find one task, or null",
                "assigned_to": "assignee used to find the task, or null"
            },
            "filters": {
                "status": "PENDING, IN_PROGRESS, COMPLETED, or null",
                "assigned_to": "assignee filter, or null",
                "title_contains": "part of the title to filter by, or null"
            },
            "requested_field": "one semantic field id or null"
        }

        Rules:
        1. "find" is used when the user asks about ONE task.
        2. "filters" is used when the user asks for a list of tasks.
        3. Copy the title and assignee text as the employee said it. Do not
           rewrite or invent names.
        4. If the user says "my" without a name, leave the assignee null.
        5. When the message asks about one specific property of a task, set
           "requested_field" to exactly one of:
           progress, status, deadline_date, deadline_time, deadline,
           reminder_date, reminder_time, reminder, assigned_to, assigned_date,
           assigned_time, task_title, id, full.
           Use "deadline" for a general due-date question, "reminder" for a
           general reminder question, and "full" only when the employee asks
           for all details. Otherwise return null.
        6. Never return a SQL column name that is not in that list. Never
           invent a value.
        7. Do not invent values. If something is not stated, return null.
        8. Understand English and Hinglish.
        9. Return exactly one JSON object, with no text or Markdown.
        """

        return self._extract_model(system_prompt, user_input, TaskQuery)


if __name__ == "__main__":
    extractor = TaskExtractor()

    user_input = (
        "Rahul has been assigned a Python API research task "
        "on 10 November 2026 at 10:30 AM. "
        "Deadline is 15 November 2026 at 5 PM. "
        "He has completed 50 percent of the work."
    )

    try:
        result = extractor.extract_task(user_input)
        print(result.model_dump(mode="json"))
    except Exception as error:
        print(error)
