"""Deterministic date arithmetic rules.

Every function here is pure: same inputs -> same output. No clocks, no
network, no LLM.

Weekday conventions (documented and tested):

    bare "Friday"       nearest occurrence STRICTLY AFTER the reference day;
                        never today (so Friday said on a Friday means +7).
    "next Friday"       that weekday in the NEXT ISO week (the week starting
                        the coming Monday). May be further away than bare.
    "this Friday"       that weekday in the CURRENT ISO week (Monday-Sunday).
                        May be today or earlier in the week.
    "last Friday"       most recent occurrence STRICTLY BEFORE the reference
                        day; never today (Friday said on a Friday means -7).

Missing-year rule for explicit dates ("November 15"):

    Use the reference year if that date still lies on or after the reference
    day, otherwise the following year. Repeat yearly (max 9 tries, enough to
    cross any leap-year gap) so 29 February resolves to the next leap year.
    Every inferred year is flagged as an assumption "year_inferred:<year>".

Numeric dates ("15/11/2026") are resolved in resolver.py because day/month
order can be ambiguous and is a caller-level configuration.
"""

from __future__ import annotations

from datetime import date, timedelta

from datetime_resolver.parsing import NumericDateToken  # noqa: F401 (re-exported for typing)


RELATIVE_OFFSETS = {
    "today": 0,
    "tomorrow": 1,
    "yesterday": -1,
    "day after tomorrow": 2,
    "day before yesterday": -2,
}


def resolve_relative(word: str, ref: date) -> date:
    """today/tomorrow/yesterday/... -> concrete date via fixed day offsets."""
    return ref + timedelta(days=RELATIVE_OFFSETS[word])


def resolve_weekday(target: int, qualifier: str | None, ref: date) -> date:
    """Resolve a weekday (0=Monday..6=Sunday) with its next/last/this qualifier."""
    if qualifier == "last":
        days_back = (ref.weekday() - target) % 7 or 7
        return ref - timedelta(days=days_back)

    if qualifier == "this":
        monday = ref - timedelta(days=ref.weekday())
        return monday + timedelta(days=target)

    if qualifier == "next":
        next_monday = ref - timedelta(days=ref.weekday()) + timedelta(days=7)
        return next_monday + timedelta(days=target)

    # bare weekday: strictly future, never today
    days_ahead = (target - ref.weekday()) % 7 or 7
    return ref + timedelta(days=days_ahead)


def build_explicit(month: int, day: int, year: int | None, ref: date) -> tuple[date, list[str]]:
    """Resolve an explicit month/day (+ optional year).

    Returns (date, assumptions). Raises ValueError for impossible dates.
    A missing year follows the nearest-future rule and is flagged.
    """
    if year is not None:
        try:
            return date(year, month, day), []
        except ValueError as error:
            raise ValueError(f"Invalid date {year}-{month:02d}-{day:02d}: {error}") from error

    for candidate_year in range(ref.year, ref.year + 9):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue                      # e.g. 29 February in a non-leap year
        if candidate >= ref:
            return candidate, [f"year_inferred:{candidate_year}"]

    raise ValueError(f"No valid year found for {month:02d}-{day:02d}")


def resolve_numeric(
    first: int, second: int, year: int, ref: date, date_format: str | None
) -> tuple[date, list[str]] | None:
    """Resolve a numeric date like 15/11/2026.

    Order rules:
        date_format "DMY" or "MDY"  -> used as configured, no ambiguity.
        one part > 12               -> order is forced (15/11 can only be
                                       day 15, month 11).
        both parts <= 12            -> returns None: caller must ask.

    Raises ValueError for impossible dates (13/13, 29/02/2027, ...).
    """
    if first > 12 and second > 12:
        raise ValueError(f"Invalid numeric date: {first:02d}/{second:02d}/{year}")

    if date_format == "DMY":
        day, month = first, second
    elif date_format == "MDY":
        month, day = first, second
    elif first > 12:
        day, month = first, second          # unambiguously day-first
    elif second > 12:
        month, day = first, second          # unambiguously month-first
    else:
        return None                          # ambiguous: caller decides

    try:
        return date(year, month, day), []
    except ValueError as error:
        raise ValueError(f"Invalid date {year}-{month:02d}-{day:02d}: {error}") from error
