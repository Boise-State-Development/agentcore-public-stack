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
    Get current date and hour in US Pacific timezone (America/Los_Angeles)

    Returns:
        str: Formatted date string with timezone (e.g., "2024-01-15 (Monday) 14:00 PST")
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

            return now.strftime(f"%Y-%m-%d (%A) %H:00 {tz_abbr}")
        else:
            # Fallback to UTC if no timezone library available
            now = datetime.now(timezone.utc)
            return now.strftime("%Y-%m-%d (%A) %H:00 UTC")
    except Exception as e:
        logger.warning(f"Failed to get Pacific time: {e}, using UTC")
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d (%A) %H:00 UTC")
