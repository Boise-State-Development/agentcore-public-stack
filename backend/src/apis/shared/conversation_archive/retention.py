"""The one retention setting every copy of a conversation follows.

``CONVERSATION_RETENTION_DAYS`` (CDK ``config.conversationRetentionDays``, from
``CDK_CONVERSATION_RETENTION_DAYS``, default 365): a conversation's content is
kept for this many days after each turn, wherever it is stored
(``docs/specs/conversation-search.md`` §3). CDK applies it to Memory events and
the archive's lifecycle rule; the daily reconcilers read it here.

Stdlib only: the conversation-index Lambda image carries this module.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

RETENTION_DAYS_ENV_VAR = "CONVERSATION_RETENTION_DAYS"
RETENTION_DAYS_DEFAULT = 365


def retention_days() -> int:
    """The configured retention, in whole days.

    Unset or empty means the default, because an unset GitHub variable arrives
    as ``""``. Anything else that is not a positive whole number **raises**
    rather than falling back: the callers delete what is older than this, and a
    typo silently read as some other number would delete on the wrong clock.
    """
    raw = os.environ.get(RETENTION_DAYS_ENV_VAR, "").strip()
    if not raw:
        return RETENTION_DAYS_DEFAULT
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{RETENTION_DAYS_ENV_VAR} must be a positive whole number of days, got {raw!r}")
    return int(raw)


def retention_cutoff(now: Optional[datetime] = None, days: Optional[int] = None) -> datetime:
    """The instant before which content is past retention (timezone-aware UTC)."""
    moment = now or datetime.now(timezone.utc)
    return moment - timedelta(days=retention_days() if days is None else days)


__all__ = [
    "RETENTION_DAYS_DEFAULT",
    "RETENTION_DAYS_ENV_VAR",
    "retention_cutoff",
    "retention_days",
]
