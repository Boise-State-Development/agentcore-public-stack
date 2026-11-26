"""A quiet watcher's state, kept beside its schedule (docs/specs/quiet-watcher.md §3.2).

One item per watcher in the sessions-metadata table::

    PK: USER#{user_id} | SK: SCHEDSTATE#{schedule_id}

``cursors`` and ``tracking`` are what the model reported at the end of its
last run (through ``report``, PR 1.2); ``seen`` is the finding keys the worker
has already notified about, newest last, so a finding is never announced
twice. ``version`` makes every write conditional, so two runs of the same
watcher can never both advance it.

While ``WATCH_TRIAGE_ENABLED`` is on, a completed full run also writes what the
triage stage needs (docs/specs/quiet-watcher-triage.md §3.4): ``probe`` (the
run's read-only calls, each ``{name, input, fingerprint}`` with ``input`` as
canonical JSON text), ``fingerprintVersion``, ``probeRunId``,
``lastFullRunAt`` and ``wakeAt``. They are worker bookkeeping, so they are
excluded from the model's dump and never reach ``GET /schedules/{id}/state``.

``pausedSessionId`` and ``pausedUntil`` are set while a run of the watcher
waits on the user (docs/specs/answer-from-inbox.md §3.2): the watcher **holds**,
skipping its runs, until that pause is answered or expires. They are written
and cleared on their own, never by :func:`save_watch_state`, so a state save
neither sets nor clears a hold.

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
from .watch_probe import MAX_PROBE_CALLS, MAX_PROBE_INPUT_CHARS, FullRunTriage

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
    # Triage bookkeeping (triage spec §3.4), read here and written only through
    # ``save_watch_state(triage=…)``. Excluded from dumps, so the state route never
    # shows them and the round trip in ``save_watch_state`` never rewrites them.
    probe: Optional[List[Dict[str, str]]] = Field(None, exclude=True)
    fingerprint_version: Optional[int] = Field(None, alias="fingerprintVersion", exclude=True)
    probe_run_id: Optional[str] = Field(None, alias="probeRunId", exclude=True)
    last_full_run_at: Optional[str] = Field(None, alias="lastFullRunAt", exclude=True)
    wake_at: Optional[str] = Field(None, alias="wakeAt", exclude=True)
    paused_session_id: Optional[str] = Field(
        None, alias="pausedSessionId", description="The run's conversation that waits on the user"
    )
    paused_until: Optional[str] = Field(
        None, alias="pausedUntil", description="When that pause expires; the hold ends then at the latest"
    )

    def holding(self, now: Optional[datetime] = None) -> bool:
        """Whether a hold is set and has not passed ``pausedUntil``. Says nothing about the pause itself."""
        if not self.paused_session_id or not self.paused_until:
            return False
        try:
            until = datetime.fromisoformat(self.paused_until.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > (now or datetime.now(timezone.utc))

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

    @field_validator("fingerprint_version", mode="before")
    @classmethod
    def _int_fingerprint_version(cls, value: object) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @field_validator("probe", mode="before")
    @classmethod
    def _whole_probe(cls, value: object) -> Optional[List[Dict[str, str]]]:
        """The probe as stored, or ``None``: a probe that breaks a bound is no probe, never part of one."""
        if not isinstance(value, (list, tuple)) or not value or len(value) > MAX_PROBE_CALLS:
            return None
        probe: List[Dict[str, str]] = []
        for call in value:
            if not isinstance(call, dict):
                return None
            name, tool_input, fingerprint = call.get("name"), call.get("input"), call.get("fingerprint")
            if not all(isinstance(v, str) and v for v in (name, tool_input, fingerprint)):
                return None
            if len(tool_input) > MAX_PROBE_INPUT_CHARS:
                return None
            probe.append({"name": name, "input": tool_input, "fingerprint": fingerprint})
        return probe


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
    triage: Optional[FullRunTriage] = None,
    clear_pause: bool = False,
) -> bool:
    """Write ``state`` if nobody else has since the caller read it. Returns whether it wrote.

    ``expected_version`` is the version the caller read, or ``None`` for a
    watcher with no state yet. The write sets the version to one more than
    that, so a second writer holding the same read loses. ``state.version`` is
    ignored. A conditional failure returns ``False``; any other error raises.

    ``triage`` is what a completed full run leaves for the triage stage, in the
    same conditional write (triage spec §3.4): each field set, or removed when
    the run left none (a run with no probe removes the probe, its version and
    its run id). ``None`` leaves those attributes as they are.

    ``clear_pause`` removes the hold in the same write: a run resumed from the inbox
    (answer-from-inbox §3.6) saves its cursors and ends the wait together.
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

    remove_parts: List[str] = []
    if triage is not None:
        fields = {
            "probe": WatchState.model_validate({"probe": triage.probe}).probe,
            "fingerprintVersion": triage.fingerprint_version,
            "probeRunId": triage.probe_run_id,
            "lastFullRunAt": triage.last_full_run_at,
            "wakeAt": triage.wake_at,
        }
        if fields["probe"] is None:
            # One unit: a probe without its version or run id can't be compared.
            fields["fingerprintVersion"] = fields["probeRunId"] = None
        for attribute, value in fields.items():
            if value is None:
                remove_parts.append(attribute)
            else:
                set_parts.append(f"{attribute} = :{attribute}")
                values[f":{attribute}"] = value

    if clear_pause:
        remove_parts += ["pausedSessionId", "pausedUntil"]

    if expected_version is None:
        condition = "attribute_not_exists(PK)"
    else:
        condition = "#version = :expected"
        values[":expected"] = expected_version

    try:
        _service._get_table().update_item(
            Key=_key(user_id, schedule_id),
            UpdateExpression="SET " + ", ".join(set_parts) + (" REMOVE " + ", ".join(remove_parts) if remove_parts else ""),
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


def set_watch_pause(
    user_id: str,
    schedule_id: str,
    *,
    session_id: str,
    paused_until: str,
    expected_version: Optional[int],
) -> bool:
    """Hold the watcher on ``session_id``'s pause, if nobody wrote since the caller read.

    Leaves the cursors, tracking and ``seen`` alone, and bumps the version like
    every conditional write. ``expected_version`` is ``None`` for a watcher with
    no state yet. Returns whether it wrote; any error but the condition raises.
    """
    from botocore.exceptions import ClientError

    values: Dict[str, object] = {
        ":session": session_id,
        ":until": paused_until,
        ":version": (expected_version or 0) + 1,
        ":updated_at": _now_iso(),
    }
    if expected_version is None:
        condition = "attribute_not_exists(PK)"
    else:
        condition = "#version = :expected"
        values[":expected"] = expected_version
    try:
        _service._get_table().update_item(
            Key=_key(user_id, schedule_id),
            UpdateExpression=(
                "SET pausedSessionId = :session, pausedUntil = :until, #version = :version, updatedAt = :updated_at"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("Watcher state for schedule %s moved past version %s; hold not set", schedule_id, expected_version)
            return False
        raise


def clear_watch_pause(user_id: str, schedule_id: str, *, session_id: str) -> bool:
    """End the hold on ``session_id``'s pause. Returns whether it cleared one.

    Conditional on the hold still naming that session, so a newer hold is never
    cleared by mistake. The version is left alone: the run that follows saves
    its state against the version it read before this.
    """
    from botocore.exceptions import ClientError

    try:
        _service._get_table().update_item(
            Key=_key(user_id, schedule_id),
            UpdateExpression="REMOVE pausedSessionId, pausedUntil",
            ConditionExpression="pausedSessionId = :session",
            ExpressionAttributeValues={":session": session_id},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def clear_watch_state(user_id: str, schedule_id: str) -> bool:
    """Delete the watcher's state ("Start over"), so its next run reads everything as new.

    Idempotent: clearing a watcher with no state is not an error.
    """
    _service._get_table().delete_item(Key=_key(user_id, schedule_id))
    return True
