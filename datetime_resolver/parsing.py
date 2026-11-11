"""Lexical parsing: find raw date/time expressions in text.

This layer only *finds candidates* and records where they occur. It never
decides what an expression means - interpretation lives in rules.py, and the
combination policy lives in resolver.py.

Supported candidates:
    Relative words:  today, tomorrow, yesterday, day after tomorrow,
                     day before yesterday
    Weekdays:        monday..sunday, optionally prefixed with next/last/this
    Explicit dates:  "November 15", "15 November 2026", "Nov 15th",
                     ISO "2026-11-15"
    Numeric dates:   "15/11/2026", "15-11-2026" (day/month order is decided
                     later, because it can be ambiguous)
    Times:           "4 PM", "4pm", "16:00", "10:30 AM", "noon", "midnight"
    Ambiguous times: "at 5" (no AM/PM and no colon)

Anything else is ignored: the resolver reports NOT_FOUND rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# --- month / weekday lookup tables -------------------------------------------

MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

WEEKDAYS: dict[str, int] = {  # Monday == 0, matching date.weekday()
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_WEEKDAY = r"monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun"


# --- compiled patterns ---------------------------------------------------------

_RELATIVE = re.compile(
    r"\b(?P<rel>day after tomorrow|day before yesterday|today|tomorrow|yesterday)\b",
    re.IGNORECASE,
)

_WEEKDAY_RE = re.compile(
    rf"\b(?:(?P<qual>next|last|this)\s+)?(?P<wd>{_WEEKDAY})\b",
    re.IGNORECASE,
)

_EXPLICIT_DATE = re.compile(
    rf"\b(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MONTH})"
    rf"|(?P<m2>{_MONTH})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")

_NUMERIC_DATE = re.compile(r"(?<!\d)(?P<a>\d{1,2})[/](?P<b>\d{1,2})[/](?P<year>\d{4})\b")

_TIME_FIXED = re.compile(r"\b(?P<fixed>noon|midnight)\b", re.IGNORECASE)

_TIME_12H = re.compile(
    r"(?<![\d:.])(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<mer>am|pm|a\.m\.|p\.m\.)",
    re.IGNORECASE,
)

_TIME_24H = re.compile(r"(?<![\d:.])(?P<hour>\d{1,2}):(?P<minute>\d{2})(?!\d)")

# "at 5" with no AM/PM and no colon: not decidable, must be asked about.
_TIME_BARE = re.compile(
    r"\bat\s+(?P<hour>\d{1,2})(?!\d)(?!\s*:\d)(?!\s*(?:am|pm|a\.m\.|p\.m\.))",
    re.IGNORECASE,
)


# --- tokens ---------------------------------------------------------------------

@dataclass(frozen=True)
class RelativeDateToken:
    raw: str
    pos: int
    word: str


@dataclass(frozen=True)
class WeekdayToken:
    raw: str
    pos: int
    weekday: int
    qualifier: str | None      # "next" | "last" | "this" | None


@dataclass(frozen=True)
class ExplicitDateToken:
    raw: str
    pos: int
    month: int
    day: int
    year: int | None


@dataclass(frozen=True)
class NumericDateToken:
    raw: str
    pos: int
    first: int
    second: int
    year: int


@dataclass(frozen=True)
class TimeToken:
    raw: str
    pos: int
    hour: int
    minute: int


@dataclass
class ParsedText:
    dates: list = field(default_factory=list)          # date tokens, text order
    times: list = field(default_factory=list)          # concrete TimeTokens
    ambiguous_times: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)   # unusable expressions


def _free(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """True when (start, end) does not overlap any already-claimed span."""
    return all(end <= s or start >= e for s, e in spans)


def parse(text: str) -> ParsedText:
    out = ParsedText()
    date_spans: list[tuple[int, int]] = []
    time_spans: list[tuple[int, int]] = []

    # --- dates (relative words first, so "day after tomorrow" is one token) ---
    for m in _RELATIVE.finditer(text):
        if _free(date_spans, m.start(), m.end()):
            date_spans.append((m.start(), m.end()))
            out.dates.append(RelativeDateToken(m.group(0), m.start(), m.group("rel").lower()))

    for m in _WEEKDAY_RE.finditer(text):
        if _free(date_spans, m.start(), m.end()):
            date_spans.append((m.start(), m.end()))
            qualifier = m.group("qual")
            out.dates.append(WeekdayToken(
                m.group(0), m.start(),
                weekday=WEEKDAYS[m.group("wd").lower()],
                qualifier=qualifier.lower() if qualifier else None,
            ))

    for m in _EXPLICIT_DATE.finditer(text):
        if _free(date_spans, m.start(), m.end()):
            date_spans.append((m.start(), m.end()))
            day = int(m.group("d1") or m.group("d2"))
            month = MONTHS[(m.group("m1") or m.group("m2")).lower()]
            year = int(m.group("year")) if m.group("year") else None
            out.dates.append(ExplicitDateToken(m.group(0), m.start(), month, day, year))

    for m in _ISO_DATE.finditer(text):
        if _free(date_spans, m.start(), m.end()):
            date_spans.append((m.start(), m.end()))
            out.dates.append(ExplicitDateToken(
                m.group(0), m.start(),
                int(m.group("month")), int(m.group("day")), int(m.group("year")),
            ))

    for m in _NUMERIC_DATE.finditer(text):
        if _free(date_spans, m.start(), m.end()):
            date_spans.append((m.start(), m.end()))
            out.dates.append(NumericDateToken(
                m.group(0), m.start(),
                int(m.group("a")), int(m.group("b")), int(m.group("year")),
            ))

    # --- times ---------------------------------------------------------------
    fixed = {"noon": (12, 0), "midnight": (0, 0)}
    for m in _TIME_FIXED.finditer(text):
        if _free(time_spans, m.start(), m.end()):
            time_spans.append((m.start(), m.end()))
            hour, minute = fixed[m.group("fixed").lower()]
            out.times.append(TimeToken(m.group(0), m.start(), hour, minute))

    for m in _TIME_12H.finditer(text):
        if _free(time_spans, m.start(), m.end()):
            time_spans.append((m.start(), m.end()))
            hour = int(m.group("hour"))
            meridiem = m.group("mer").replace(".", "").lower()   # "a.m." -> "am"
            if not 1 <= hour <= 12:
                out.invalid.append(f"Invalid 12-hour time: '{m.group(0).strip()}'")
                continue
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            out.times.append(TimeToken(m.group(0), m.start(), hour, int(m.group("minute") or 0)))

    for m in _TIME_24H.finditer(text):
        if _free(time_spans, m.start(), m.end()):
            time_spans.append((m.start(), m.end()))
            hour, minute = int(m.group("hour")), int(m.group("minute"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                out.invalid.append(f"Invalid 24-hour time: '{m.group(0)}'")
                continue
            out.times.append(TimeToken(m.group(0), m.start(), hour, minute))

    for m in _TIME_BARE.finditer(text):
        if _free(time_spans, m.start(), m.end()):
            time_spans.append((m.start(), m.end()))
            out.ambiguous_times.append(m.group(0).strip())

    return out
