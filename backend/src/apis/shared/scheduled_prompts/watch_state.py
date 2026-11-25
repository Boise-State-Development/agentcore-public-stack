"""A quiet watcher's state, kept beside its schedule (docs/specs/quiet-watcher.md §3.2).

One item per watcher in the sessions-metadata table::

    PK: USER#{user_id} | SK: SCHEDSTATE#{schedule_id}

``cursors`` and ``tracking`` are what the model reported at the end of its
last run (through ``report``, PR 1.2); ``seen`` is the finding keys the worker
has already notified about, newest last, so a finding is never announced
twice. ``version`` makes every write conditional, so two runs of the same
watcher can never both advance it.

The bounds are enforced by truncating, never by raising: the state is written
from a model's report, and a report that overshoots must still save what fits
rather than lose the whole run's progress.

Every function here is blocking (one DynamoDB call). app-api calls them with
``asyncio.to_thread``; the scheduled-runs worker does the same.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import service as _service

logger = logging.getLogger(__name__)

MAX_CURSORS = 20
MAX_TRACKING = 20
MAX_SEEN = 500
#: The longest cursor name, cursor value, tracking entry or seen key kept.
MAX_TEXT_CHARS = 200

STATE_SK_PREFIX = "SCHEDSTATE#"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _key(user_id: str, schedule_id: str) -> Dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": f"{STATE_SK_PREFIX}{schedule_id}"}


def _text(value: object) -> str:
    return str(value)[:MAX_TEXT_CHARS]


def append_seen(seen: Iterable[str], keys: Iterable[str], cap: int = MAX_SEEN) -> List[str]:
    """``seen`` with ``keys`` appended, newest last, deduped, and cut to the newest ``cap``.

    A key already in ``seen`` moves to the end, since it was just seen again.
    Pure: neither argument is changed.
    """
    order: Dict[str, None] = {}
    for key in list(seen) + list(keys):
        if not key:
            continue
        text = _text(key)
        order.pop(text, None)
        order[text] = None
    merged = list(order)
    return merged[-cap:] if cap > 0 else []


class WatchState(BaseModel):
    """What a watcher carries from one run to the next."""

    model_config = ConfigDict(populate_by_name=True)

    cursors: Dict[str, str] = Field(default_factory=dict, description="Where to check from, by name")
    tracking: List[str] = Field(default_factory=list, description="What the watcher is keeping an eye on")
    seen: List[str] = Field(default_factory=list, description="Finding keys already notified, newest last")
    version: int = Field(0, description="Bumped on every write; a write names the version it read")
    last_run_at: Optional[str] = Field(None, alias="lastRunAt")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    @field_validator("cursors", mode="before")
    @classmethod
    def _clamp_cursors(cls, value: object) -> Dict[str, str]:
        if not isinstance(value, dict):
            return {}
        out: Dict[str, str] = {}
        for name, cursor in value.items():
            if len(out) >= MAX_CURSORS:
                break
            if cursor is None:
                continue
            out[_text(name)] = _text(cursor)
        return out

    @field_validator("tracking", mode="before")
    @classmethod
    def _clamp_tracking(cls, value: object) -> List[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [_text(entry) for entry in value if entry is not None][:MAX_TRACKING]

    @field_validator("seen", mode="before")
    @classmethod
    def _clamp_seen(cls, value: object) -> List[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return append_seen([], [str(entry) for entry in value if entry is not None])

    @field_validator("version", mode="before")
    @classmethod
    def _int_version(cls, value: object) -> int:
        # DynamoDB returns numbers as Decimal.
        return int(value) if value is not None else 0


def get_watch_state(user_id: str, schedule_id: str) -> Optional[WatchState]:
    """The watcher's state, or ``None`` when it has none (never run, or started over)."""
    item = _service._get_table().get_item(Key=_key(user_id, schedule_id)).get("Item")
    if not item:
        return None
    return WatchState.model_validate(item)


def save_watch_state(
    user_id: str,
    schedule_id: str,
    state: WatchState,
    *,
    expected_version: Optional[int],
) -> bool:
    """Write ``state`` if nobody else has since the caller read it. Returns whether it wrote.

    ``expected_version`` is the version the caller read, or ``None`` for a
    watcher with no state yet. The write sets the version to one more than
    that, so a second writer holding the same read loses. ``state.version`` is
    ignored. A conditional failure returns ``False``; any other error raises.
    """
    from botocore.exceptions import ClientError

    clamped = WatchState.model_validate(state.model_dump(by_alias=True))
    now = _now_iso()
    new_version = (expected_version or 0) + 1
    values: Dict[str, object] = {
        ":cursors": clamped.cursors,
        ":tracking": clamped.tracking,
        ":seen": clamped.seen,
        ":version": new_version,
        ":updated_at": now,
    }
    set_parts = [
        "cursors = :cursors",
        "tracking = :tracking",
        "seen = :seen",
        "#version = :version",
        "updatedAt = :updated_at",
    ]
    if clamped.last_run_at:
        set_parts.append("lastRunAt = :last_run_at")
        values[":last_run_at"] = clamped.last_run_at

    if expected_version is None:
        condition = "attribute_not_exists(PK)"
    else:
        condition = "#version = :expected"
        values[":expected"] = expected_version

    try:
        _service._get_table().update_item(
            Key=_key(user_id, schedule_id),
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(
                "Watcher state for schedule %s moved past version %s; not saved", schedule_id, expected_version
            )
            return False
        raise


def clear_watch_state(user_id: str, schedule_id: str) -> bool:
    """Delete the watcher's state ("Start over"), so its next run reads everything as new.

    Idempotent: clearing a watcher with no state is not an error.
    """
    _service._get_table().delete_item(Key=_key(user_id, schedule_id))
    return True
