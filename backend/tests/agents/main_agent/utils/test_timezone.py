"""
Tests for timezone utilities.
Requirements: 21.1–21.3

The rendered string is part of the Bedrock prompt-cache prefix, so it must be
byte-stable within a Pacific day: date, weekday and timezone only — no hour.
"""

import re
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agents.main_agent.utils.timezone import get_current_date_pacific


# Regex for "YYYY-MM-DD (DayName) TZ"
DATE_FORMAT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} "           # YYYY-MM-DD
    r"\([A-Z][a-z]+\) "              # (DayName)
    r"[A-Z]{3,4}$"                   # TZ abbreviation
)

VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}

PACIFIC = ZoneInfo("America/Los_Angeles")


class _FrozenDatetime(datetime):
    """datetime subclass whose now() returns a fixed instant (in the requested tz)."""

    frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


def _at(instant: datetime) -> str:
    """Call get_current_date_pacific() as if it were `instant` right now."""
    _FrozenDatetime.frozen = instant
    with patch("agents.main_agent.utils.timezone.datetime", _FrozenDatetime):
        return get_current_date_pacific()


class TestGetCurrentDatePacific:
    """Req 21.1: Verify format matches 'YYYY-MM-DD (DayName) TZ'."""

    def test_format_matches_expected_pattern(self):
        result = get_current_date_pacific()
        assert DATE_FORMAT_RE.match(result), f"Output '{result}' does not match expected format"

    def test_contains_valid_day_name(self):
        result = get_current_date_pacific()
        day = re.search(r"\((\w+)\)", result).group(1)
        assert day in VALID_DAYS, f"Day '{day}' is not a valid day name"

    def test_has_no_time_of_day_component(self):
        # Prompt-cache contract: the line must not change at hour boundaries.
        result = get_current_date_pacific()
        assert not re.search(r"\d{1,2}:\d{2}", result), f"Unexpected time-of-day in '{result}'"
        assert len(result.split()) == 3


class TestDayStability:
    """The rendered string must be identical for every hour of the same Pacific day
    and change only when the Pacific calendar date changes."""

    def test_identical_across_every_hour_of_a_day(self):
        base = datetime(2026, 9, 15, 0, 0, tzinfo=PACIFIC)
        rendered = {_at(base.replace(hour=h, minute=m)) for h in range(24) for m in (0, 59)}
        assert rendered == {"2026-09-15 (Tuesday) PDT"}, rendered

    def test_changes_at_pacific_midnight(self):
        before = _at(datetime(2026, 9, 15, 23, 59, tzinfo=PACIFIC))
        after = _at(datetime(2026, 9, 16, 0, 0, tzinfo=PACIFIC))
        assert before == "2026-09-15 (Tuesday) PDT"
        assert after == "2026-09-16 (Wednesday) PDT"

    def test_uses_pacific_date_not_utc_date(self):
        # 03:00 UTC on the 16th is still 20:00 PDT on the 15th.
        utc_instant = datetime(2026, 9, 16, 3, 0, tzinfo=ZoneInfo("UTC"))
        assert _at(utc_instant) == "2026-09-15 (Tuesday) PDT"


class TestTimezoneAbbreviation:
    """Req 21.2: Verify timezone abbreviation is PST or PDT."""

    def test_timezone_is_pst_or_pdt(self):
        result = get_current_date_pacific()
        tz = result.split()[-1]
        assert tz in ("PST", "PDT"), f"Timezone '{tz}' is not PST or PDT"

    def test_standard_time_renders_pst(self):
        assert _at(datetime(2026, 1, 15, 12, 0, tzinfo=PACIFIC)) == "2026-01-15 (Thursday) PST"


class TestUTCFallback:
    """Req 21.3: Verify UTC fallback when timezone libraries unavailable."""

    def test_falls_back_to_utc_when_timezone_unavailable(self):
        with patch("agents.main_agent.utils.timezone.TIMEZONE_AVAILABLE", False):
            result = get_current_date_pacific()
        assert result.endswith("UTC"), f"Expected UTC fallback, got '{result}'"
        assert DATE_FORMAT_RE.match(result), f"UTC fallback '{result}' does not match format"

    def test_utc_fallback_format_matches(self):
        with patch("agents.main_agent.utils.timezone.TIMEZONE_AVAILABLE", False):
            result = get_current_date_pacific()
        # Should still have YYYY-MM-DD (DayName) UTC — and no hour
        parts = result.split()
        assert len(parts) == 3
        assert parts[2] == "UTC"
