"""Public entry point of the date/time resolver.

    from datetime_resolver import resolve

    result = resolve("Deadline is November 15 at 4 PM")
    data = result.to_dict()          # JSON-serialisable dict

Determinism contract:
    resolve(text, now=N, timezone=T) is a pure function of its arguments.
    The current date/time comes ONLY from the injected `now` or the system
    clock - never from any language model - so results are reproducible.

Policy summary (details in rules.py):

    * Dates and times are never invented: a missing part becomes
      NEEDS_CLARIFICATION while every successfully resolved part is
      preserved (date-only input keeps resolved_date and weekday).
    * Bare "Friday" = nearest strictly-future occurrence (never today);
      "next Friday" = that weekday in the next ISO week;
      "this Friday" = current ISO week; "last Friday" = strictly past.
    * Numeric dates like 05/06/2026 with both parts <= 12 are ambiguous and
      return NEEDS_CLARIFICATION unless the caller configures
      date_format="DMY" or "MDY". Unambiguous cases (15/11, ISO dates)
      resolve directly.
    * An omitted year is inferred as the nearest occurrence on/after today
      and always flagged in `assumptions`.
    * Invalid inputs ("February 30", "25:00", "13 PM") return INVALID,
      never a repaired guess.
    * Colon times without meridiem ("16:00") are 24-hour.
"""

from __future__ import annotations

from datetime_resolver.models import Clarification, ResolutionResult, ResolutionStatus
from datetime_resolver.resolver import DEFAULT_TIMEZONE, DateResolver, resolve

__all__ = [
    "DEFAULT_TIMEZONE",
    "Clarification",
    "DateResolver",
    "ResolutionResult",
    "ResolutionStatus",
    "resolve",
]
