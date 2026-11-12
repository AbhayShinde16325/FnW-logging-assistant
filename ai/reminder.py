"""Reminder parsing: absolute times or offsets relative to the deadline.

Both forms resolve to one uniform result: an absolute (date, time) pair.

    parse_reminder("30 minutes before the deadline", deadline_date=..., deadline_time=...)
        -> (date(2026, 11, 15), time(16, 30))

    parse_reminder("15 November 2026 at 4 PM", ...)
        -> (date(2026, 11, 15), time(16, 0))

All date/time interpretation is delegated to the deterministic
`datetime_resolver` package (never the LLM, never guessed).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from datetime_resolver import ResolutionStatus, resolve
from datetime_resolver import parsing as dt_parsing


class ReminderError(ValueError):
    """User-facing reminder problem.

    `resolved_date` / `resolved_time` carry any partial resolution so the
    caller can ask a targeted follow-up and pass the parts back via
    `context` on the next attempt.
    """

    def __init__(self, message, resolved_date=None, resolved_time=None):
        super().__init__(message)
        self.resolved_date = resolved_date
        self.resolved_time = resolved_time


# --- relative offsets ("30 minutes before the deadline") ----------------------

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "thirty": 30, "half": 0.5,
}

_OFFSET_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|thirty|half)\s*"
    r"(?P<unit>min(?:ute)?s?|m|hours?|hrs?|h|days?|d|weeks?|w)\s+"
    r"(?:before|prior to)\s+(?:the\s+)?deadline",
    re.IGNORECASE,
)

_AT_DEADLINE_RE = re.compile(
    r"(?:at|on|by|when)\s+the\s+deadline|at\s+deadline\s+time",
    re.IGNORECASE,
)


def _offset_minutes(number: str, unit: str) -> int:
    if re.fullmatch(r"\d+(?:\.\d+)?", number):
        value = float(number)
    else:
        value = _WORD_NUMBERS[number.lower()]

    unit = unit.lower()
    if unit.startswith("min") or unit == "m":
        factor = 1
    elif unit.startswith("h"):
        factor = 60
    elif unit.startswith("d"):
        factor = 24 * 60
    else:  # weeks
        factor = 7 * 24 * 60

    return int(value * factor)


# --- public API ----------------------------------------------------------------

def has_date_component(text: str) -> bool:
    """True if the text contains any recognisable date expression."""
    return bool(dt_parsing.parse(text or "").dates)


def has_time_component(text: str) -> bool:
    """True if the text contains any recognisable time expression."""
    parsed = dt_parsing.parse(text or "")
    return bool(parsed.times or parsed.ambiguous_times)


def parse_reminder(
    text: str,
    *,
    deadline_date: date,
    deadline_time: time,
    now: datetime | None = None,
    timezone: str = "Asia/Kolkata",
    context: dict | None = None,
) -> tuple[date, time]:
    """Resolve a reminder description to an absolute (date, time).

    Raises ReminderError with a user-facing message when the input is
    ambiguous, invalid, in the past, or after the deadline.
    """
    if deadline_date is None or deadline_time is None:
        raise ReminderError(
            "The deadline must be set before choosing a reminder."
        )

    tzinfo = ZoneInfo(timezone)

    if now is None:
        now = datetime.now(tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)

    deadline_dt = datetime.combine(deadline_date, deadline_time)

    cleaned = (text or "").strip()
    if not cleaned:
        raise ReminderError(
            "Please say when you want the reminder (e.g. '30 minutes before "
            "the deadline' or '15 November 2026 at 4 PM')."
        )

    # Merge partials from a previous clarification round.
    if context:
        context_date = context.get("date")
        context_time = context.get("time")
        if context_date and not has_date_component(cleaned):
            if context_time and not has_time_component(cleaned):
                cleaned = f"{context_date} {context_time}"
            else:
                cleaned = f"{context_date} {cleaned}"

    lowered = cleaned.lower()
    normalized = re.sub(r"half\s+an\s+hour", "30 minutes", lowered)

    reminder_dt: datetime | None = None

    offset_match = _OFFSET_RE.search(normalized)
    if offset_match:
        minutes = _offset_minutes(
            offset_match.group("num"), offset_match.group("unit")
        )
        reminder_dt = deadline_dt - timedelta(minutes=minutes)
    elif _AT_DEADLINE_RE.search(normalized) or normalized in {"the deadline", "deadline"}:
        reminder_dt = deadline_dt
    else:
        result = resolve(cleaned, now=now, timezone=timezone)

        if (
            result.status == ResolutionStatus.RESOLVED
            and result.resolved_date
            and result.resolved_time
        ):
            reminder_dt = datetime.combine(
                result.resolved_date, result.resolved_time
            )
        elif result.status == ResolutionStatus.NEEDS_CLARIFICATION:
            raise ReminderError(
                result.clarification.question,
                resolved_date=result.resolved_date,
                resolved_time=result.resolved_time,
            )
        elif result.status == ResolutionStatus.INVALID:
            raise ReminderError(result.error or "Invalid date or time.")
        else:
            raise ReminderError(
                "Could not understand the reminder time. Give a date and time "
                "(e.g. '15 November 2026 at 4 PM') or say how long before the "
                "deadline (e.g. '30 minutes before the deadline')."
            )

    reminder_aware = reminder_dt.replace(tzinfo=tzinfo)
    deadline_aware = deadline_dt.replace(tzinfo=tzinfo)

    if reminder_aware <= now:
        raise ReminderError(
            "That reminder time is in the past. Please choose a future time."
        )

    if reminder_aware > deadline_aware:
        raise ReminderError(
            "The reminder cannot be after the deadline. Please choose an "
            "earlier time."
        )

    return reminder_dt.date(), reminder_dt.time()
