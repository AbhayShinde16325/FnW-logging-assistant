import json
import requests

from datetime import date
from pydantic import BaseModel, Field, field_validator


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "qwen3:4b"


class TaskDetails(BaseModel):
    task_title: str = Field(min_length=1)
    deadline: date | None = None
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


class TaskExtractor:

    def extract_task(self, user_input):
        system_prompt = """
        You are a task information extraction assistant.

        Extract task information from the user's input.

        Return ONLY valid JSON with these fields:
        {
            "task_title": "string",
            "deadline": "YYYY-MM-DD or null",
            "status": "PENDING, IN_PROGRESS, or COMPLETED",
            "progress": 0
        }

        Rules:
        1. Do not invent missing information.
        2. If the deadline is missing, return null.
        3. If progress is missing, use 0 unless completed.
        4. If completed, progress must be 100.
        5. Progress must be between 0 and 100.
        6. If progress is above 0, status should normally be IN_PROGRESS.
        7. Understand English and Hinglish.
        8. Return JSON only, without Markdown.
        """

        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0
            }
        }

        try:
            response = requests.post(
                OLLAMA_URL,
                json=payload,
                timeout=120
            )

            response.raise_for_status()

            response_data = response.json()
            llm_output = response_data["message"]["content"]

            extracted_data = json.loads(llm_output)
            task = TaskDetails.model_validate(extracted_data)

            if task.status == "COMPLETED" and task.progress != 100:
                raise ValueError(
                    "Completed tasks must have 100% progress."
                )

            if task.progress > 0 and task.status == "PENDING":
                raise ValueError(
                    "Tasks with progress must be IN_PROGRESS or COMPLETED."
                )

            return task

        except requests.RequestException as error:
            raise RuntimeError(
                f"Could not connect to Ollama: {error}"
            ) from error

        except (json.JSONDecodeError, KeyError) as error:
            raise RuntimeError(
                f"Invalid response from the AI model: {error}"
            ) from error


if __name__ == "__main__":
    extractor = TaskExtractor()

    user_input = (
        "I have been assigned a Python API research task. "
        "Deadline is 15 November 2026. "
        "I have completed 50 percent of the work."
    )

    try:
        result = extractor.extract_task(user_input)
        print(result.model_dump(mode="json"))
    except Exception as error:
        print(error)