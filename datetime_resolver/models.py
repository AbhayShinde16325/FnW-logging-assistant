"""Result models for the date/time resolver.

Every public call returns a ResolutionResult whose to_dict() output contains
only JSON-serialisable primitives (str, int, list, dict, None) - never
datetime objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Any


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"


@dataclass
class Clarification:
    """Tells the caller what to ask the user instead of guessing."""

    missing: list[str]
    reason: str
    question: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "missing": list(self.missing),
            "reason": self.reason,
            "question": self.question,
        }


@dataclass
class ResolutionResult:
    status: ResolutionStatus
    original_text: str
    timezone: str
    matched_expression: str | None = None
    resolved_date: date | None = None
    resolved_time: time | None = None
    weekday: str | None = None
    iso_datetime: str | None = None
    assumptions: list[str] = field(default_factory=list)
    clarification: Clarification | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "original_text": self.original_text,
            "matched_expression": self.matched_expression,
            "resolved_date": self.resolved_date.isoformat() if self.resolved_date else None,
            "resolved_time": self.resolved_time.isoformat() if self.resolved_time else None,
            "weekday": self.weekday,
            "timezone": self.timezone,
            "iso_datetime": self.iso_datetime,
            "assumptions": list(self.assumptions),
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "error": self.error,
        }
