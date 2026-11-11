"""resolve(): combine parsed candidates into a ResolutionResult.

Policy implemented here (all deterministic):

    1. Invalid expressions ("25:00", "13 PM") -> INVALID, never repaired.
    2. More than one distinct date (or time) -> NEEDS_CLARIFICATION;
       identical duplicates ("tomorrow, 12 November 2026") count once.
    3. Ambiguous numeric dates (05/06/2026) -> NEEDS_CLARIFICATION unless
       date_format was configured ("DMY"/"MDY").
    4. Date without time -> NEEDS_CLARIFICATION (missing time) but the
       resolved date and weekday are preserved, never discarded.
    5. Time without date -> NEEDS_CLARIFICATION (missing date), time kept.
    6. Nothing recognisable -> NOT_FOUND.
    7. A bare "at 5" (no AM/PM) -> NEEDS_CLARIFICATION (ambiguous_time).
"""

from __future__ import annotations

import calendar
from datetime import datetime, time
from zoneinfo import ZoneInfo

from datetime_resolver import parsing, rules
from datetime_resolver.models import Clarification, ResolutionResult, ResolutionStatus
from datetime_resolver.parsing import (
    ExplicitDateToken,
    NumericDateToken,
    TimeToken,
    WeekdayToken,
    parse,
)
from datetime_resolver.rules import build_explicit, resolve_numeric, resolve_relative, resolve_weekday


DEFAULT_TIMEZONE = "Asia/Kolkata"
_VALID_FORMATS = (None, "DMY", "MDY")


class DateResolver:
    """Reusable resolver with configured timezone and numeric-date format."""

    def __init__(self, timezone: str = DEFAULT_TIMEZONE, date_format: str | None = None):
        if date_format not in _VALID_FORMATS:
            raise ValueError(
                f"date_format must be one of None, 'DMY', 'MDY' (got {date_format!r})"
            )
        self.timezone = timezone
        self.date_format = date_format

    def resolve(self, text: str, now: datetime | None = None) -> ResolutionResult:
        original = text if isinstance(text, str) else ""

        try:
            tzinfo = ZoneInfo(self.timezone)
        except Exception:
            return self._invalid(original, f"Unknown timezone: {self.timezone!r}")

        if not isinstance(text, str) or not text.strip():
            return self._invalid(original, "Input text is empty.")

        ref_date = self._reference(now, tzinfo).date()
        parsed = parse(text)

        if parsed.invalid:
            return self._invalid(original, parsed.invalid[0])

        resolved_dates: list[tuple[object, int, str]] = []   # (date, pos, raw)
        resolved_times: list[tuple[int, int, int, str]] = []  # (h, m, pos, raw)
        assumptions: list[str] = []
        errors: list[str] = []
        ambiguous_numeric = None

        for token in parsed.dates:
            try:
                if isinstance(token, NumericDateToken):
                    outcome = resolve_numeric(
                        token.first, token.second, token.year, ref_date, self.date_format
                    )
                    if outcome is None:          # both parts <= 12, no configured order
                        ambiguous_numeric = token
                        continue
                    value, assumed = outcome
                elif isinstance(token, ExplicitDateToken):
                    value, assumed = build_explicit(token.month, token.day, token.year, ref_date)
                elif isinstance(token, WeekdayToken):
                    value, assumed = resolve_weekday(token.weekday, token.qualifier, ref_date), []
                else:  # RelativeDateToken
                    value, assumed = resolve_relative(token.word, ref_date), []
            except ValueError as error:
                errors.append(str(error))
                continue
            resolved_dates.append((value, token.pos, token.raw))
            assumptions.extend(assumed)

        if errors:
            return self._invalid(original, errors[0])

        for token in parsed.times:
            resolved_times.append((token.hour, token.minute, token.pos, token.raw))

        if ambiguous_numeric is not None:
            token = ambiguous_numeric
            dmy_month = calendar.month_name[token.second]
            mdy_month = calendar.month_name[token.first]
            question = (
                f"'{token.raw}' is ambiguous: is it {token.first} {dmy_month} {token.year} "
                f"(day-first) or {token.second} {mdy_month} {token.year} (month-first)?"
            )
            return self._clarify(
                original, ["date"], "ambiguous_numeric_date", question,
                matched=[(token.pos, token.raw)],
            )

        # identical duplicates ("tomorrow, 12 November 2026") count once
        unique_dates = self._unique(resolved_dates, key=lambda item: item[0])
        unique_times = self._unique(resolved_times, key=lambda item: (item[0], item[1]))

        if len(unique_dates) > 1:
            raws = ", ".join(f"'{raw}'" for _, _, raw in unique_dates)
            return self._clarify(
                original, ["date"], "multiple_candidates",
                f"Multiple dates mentioned: {raws}. Which one did you mean?",
                matched=[(pos, raw) for _, pos, raw in unique_dates],
            )

        if len(unique_times) > 1:
            raws = ", ".join(f"'{raw}'" for _, _, _, raw in unique_times)
            return self._clarify(
                original, ["time"], "multiple_candidates",
                f"Multiple times mentioned: {raws}. Which one did you mean?",
                matched=[(pos, raw) for _, _, pos, raw in unique_times],
                date=unique_dates[0][0] if unique_dates else None,
                assumptions=assumptions,
            )

        resolved_date = unique_dates[0][0] if unique_dates else None
        resolved_time = time(unique_times[0][0], unique_times[0][1]) if unique_times else None
        matched = [(pos, raw) for _, pos, raw in unique_dates]
        matched += [(pos, raw) for _, _, pos, raw in unique_times]

        if resolved_date is not None and resolved_time is not None:
            return self._resolved(original, resolved_date, resolved_time, tzinfo, matched, assumptions)

        if resolved_date is not None:
            if parsed.ambiguous_times:
                matched += [(0, raw) for raw in parsed.ambiguous_times]
                return self._clarify(
                    original, ["time"], "ambiguous_time",
                    f"Did you mean AM or PM for '{parsed.ambiguous_times[0]}' "
                    f"on {resolved_date.isoformat()}?",
                    matched=matched, date=resolved_date, assumptions=assumptions,
                )
            return self._clarify(
                original, ["time"], "missing_time",
                f"What time on {resolved_date.isoformat()}?",
                matched=matched, date=resolved_date, assumptions=assumptions,
            )

        if resolved_time is not None or parsed.ambiguous_times:
            time_raw = (
                unique_times[0][3] if unique_times else parsed.ambiguous_times[0]
            )
            if resolved_time is not None:
                matched += [(unique_times[0][2], unique_times[0][3])]
            return self._clarify(
                original, ["date"], "missing_date",
                f"Which date is '{time_raw}' referring to?",
                matched=matched, time_value=resolved_time,
            )

        return ResolutionResult(
            status=ResolutionStatus.NOT_FOUND,
            original_text=original,
            timezone=self.timezone,
        )

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _reference(now: datetime | None, tzinfo: ZoneInfo) -> datetime:
        if now is None:
            return datetime.now(tzinfo)                     # the only clock access
        if now.tzinfo is None:                              # naive -> treated as wall clock in tz
            return now.replace(tzinfo=tzinfo)
        return now.astimezone(tzinfo)

    @staticmethod
    def _unique(items: list, key) -> list:
        seen: set = set()
        unique = []
        for item in items:
            identity = key(item)
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        return unique

    def _resolved(self, original, resolved_date, resolved_time, tzinfo, matched, assumptions):
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            original_text=original,
            timezone=self.timezone,
            matched_expression=self._matched(matched),
            resolved_date=resolved_date,
            resolved_time=resolved_time,
            weekday=calendar.day_name[resolved_date.weekday()],
            iso_datetime=datetime.combine(resolved_date, resolved_time, tzinfo=tzinfo).isoformat(),
            assumptions=assumptions,
        )

    def _clarify(self, original, missing, reason, question, *, matched=None, date=None,
                 time_value=None, assumptions=None):
        result = ResolutionResult(
            status=ResolutionStatus.NEEDS_CLARIFICATION,
            original_text=original,
            timezone=self.timezone,
            matched_expression=self._matched(matched or []),
            assumptions=list(assumptions or []),
            clarification=Clarification(missing=list(missing), reason=reason, question=question),
        )
        if date is not None:
            result.resolved_date = date
            result.weekday = calendar.day_name[date.weekday()]
        if time_value is not None:
            result.resolved_time = time_value
        return result

    def _invalid(self, original, error) -> ResolutionResult:
        return ResolutionResult(
            status=ResolutionStatus.INVALID,
            original_text=original,
            timezone=self.timezone,
            error=error,
        )

    @staticmethod
    def _matched(parts: list[tuple[int, str]]) -> str | None:
        if not parts:
            return None
        return " ".join(raw for _, raw in sorted(parts))


def resolve(
    text: str,
    *,
    now: datetime | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    date_format: str | None = None,
) -> ResolutionResult:
    """Module-level convenience wrapper around DateResolver."""
    return DateResolver(timezone=timezone, date_format=date_format).resolve(text, now=now)
