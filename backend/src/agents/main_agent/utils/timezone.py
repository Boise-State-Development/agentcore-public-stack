"""
Timezone utilities for agent system prompts
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Check timezone support availability (zoneinfo for Python 3.9+, fallback to pytz)
try:
    from zoneinfo import ZoneInfo  # noqa: F401
    TIMEZONE_AVAILABLE = True
except ImportError:
    try:
        from importlib.util import find_spec
        TIMEZONE_AVAILABLE = find_spec("pytz") is not None
    except Exception:
        TIMEZONE_AVAILABLE = False
    if not TIMEZONE_AVAILABLE:
        logger.warning("Neither zoneinfo nor pytz available - date will use UTC")


def get_current_date_pacific() -> str:
    """
    Get the current calendar date in US Pacific timezone (America/Los_Angeles).

    The result is rendered into the system prompt, which is the head of the
    Bedrock prompt-cache prefix (see the prompt-cache contract in CLAUDE.md).
    It therefore deliberately contains **no hour**: the string is byte-stable
    for a whole Pacific day, so the cached prefix is re-written once per day
    instead of at every hour boundary. Earlier versions appended ``HH:00`` and
    a 2026-09 prod cost audit attributed ~2.6% of cache-write spend to the
    resulting hourly ``systemPromptHash`` flips. If a flow ever needs the
    time of day, put it in the user turn (or another turn-scoped message),
    never back in this prefix.

    Returns:
        str: Date, weekday and timezone abbreviation
             (e.g., "2024-01-15 (Monday) PST")
    """
    try:
        if TIMEZONE_AVAILABLE:
            try:
                # Try zoneinfo first (Python 3.9+)
                from zoneinfo import ZoneInfo
                pacific_tz = ZoneInfo("America/Los_Angeles")
                now = datetime.now(pacific_tz)
                # Get timezone abbreviation (PST/PDT)
                tz_abbr = now.strftime("%Z")
            except (ImportError, NameError):
                # Fallback to pytz
                import pytz
                pacific_tz = pytz.timezone("America/Los_Angeles")
                now = datetime.now(pacific_tz)
                # Get timezone abbreviation (PST/PDT)
                tz_abbr = now.strftime("%Z")

            return now.strftime(f"%Y-%m-%d (%A) {tz_abbr}")
        else:
            # Fallback to UTC if no timezone library available
            now = datetime.now(timezone.utc)
            return now.strftime("%Y-%m-%d (%A) UTC")
    except Exception as e:
        logger.warning(f"Failed to get Pacific time: {e}, using UTC")
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d (%A) UTC")
