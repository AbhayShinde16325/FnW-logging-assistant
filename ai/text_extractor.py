import os
import json
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

    reminder_date / reminder_time have no database columns yet; the
    repository ignores them until the migration adds them.
    """

    reminder_date: date | None = None
    reminder_time: time | None = None


def validate_business_rules(task):
    if task.status == "COMPLETED" and task.progress != 100:
        raise ValueError(
            "Completed tasks must have 100% progress."
        )

    if task.progress > 0 and task.status == "PENDING":
        raise ValueError(
            "Tasks with progress must be IN_PROGRESS or COMPLETED."
        )


def parse_llm_json(text):
    """
    Parse the first JSON object in the model's output.

    Tolerates trailing data (a repeated object or extra text) which
    json.loads() rejects with an "Extra data" error.
    """
    text = text.strip()

    data, _ = json.JSONDecoder().raw_decode(text)

    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            "The model output is not a JSON object.", text, 0
        )

    return data


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
                response = client.chat(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_input}
                    ],
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

                raw_output = text_segments[0].text

                extracted_data = parse_llm_json(raw_output)
                task = TaskDetails.model_validate(extracted_data)

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
                response = client.chat(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_input}
                    ],
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

                raw_output = text_segments[0].text

                extracted_data = parse_llm_json(raw_output)
                return ExtractionResult.model_validate(extracted_data)

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
