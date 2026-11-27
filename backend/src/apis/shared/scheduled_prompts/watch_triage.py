"""Quiet watcher triage, tier 1 (docs/specs/quiet-watcher-triage.md §2, §4.1).

What both sides of a ``watch_triage`` invocation agree on, with no I/O:

- the request the worker sends (:class:`WatchTriageRequest`, built from the stored probe by
  :func:`triage_request_from_state`);
- when the worker sends none and goes straight to the full run (:func:`skip_reason`, §2.4);
- the compare of the replayed fingerprints with the baseline (:func:`compare_fingerprints`);
- the verdict, its ``watch_triage_result`` frame, and how the worker reads it back
  (:func:`verdict_from_frame`) and records it (:func:`history_triage`).

Every failure is an escalation (§2.5): a verdict the worker can't read is one too. Tier 2 (the
decider) is PR T.3's; until then a changed fingerprint escalates as ``changed_no_decider``.

Stdlib and pydantic only, so the scheduled-runs worker (which may import nothing but
``apis.shared``) can use it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .watch_probe import FINGERPRINT_CHARS, FINGERPRINT_VERSION, MAX_PROBE_CALLS, MAX_PROBE_INPUT_CHARS

#: The one frame a ``watch_triage`` invocation streams, before ``done``.
WATCH_TRIAGE_EVENT = "watch_triage_result"

#: Each replayed call's limit (§2.5).
REPLAY_TIMEOUT_SECONDS = 10.0

#: The floor that forces a full run however quiet the probe stays (§2.4).
DEFAULT_MAX_STALENESS_HOURS = 24
_MAX_STALENESS_ENV = "WATCH_TRIAGE_MAX_STALENESS_HOURS"

QUIET = "quiet"
ESCALATE = "escalate"
VERDICTS = (QUIET, ESCALATE)

#: Why the triage said what it did (§4.1). ``decider_*`` and ``too_large`` are tier 2's (T.3).
REASONS = (
    "unchanged",
    "changed_no_decider",
    "probe_refused",
    "probe_error",
    "flag_off",
    "too_large",
    "decider_quiet",
    "decider_escalate",
    "decider_error",
)

#: Why the worker sent no triage at all (§2.4), recorded in shadow so the readout can count them.
SKIP_REASONS = ("no_probe", "version_mismatch", "wake_due", "stale")

#: The worker could not invoke the triage or read its result (§2.5's last row).
TRIAGE_FAILED = "triage_failed"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def max_staleness_hours() -> int:
    """``WATCH_TRIAGE_MAX_STALENESS_HOURS``, default 24, kept within 1 hour to 30 days."""
    try:
        hours = int(os.environ.get(_MAX_STALENESS_ENV, "") or DEFAULT_MAX_STALENESS_HOURS)
    except ValueError:
        hours = DEFAULT_MAX_STALENESS_HOURS
    return min(max(hours, 1), 30 * 24)


class WatchTriageCall(BaseModel):
    """One stored probe call: the tool's name and its input as canonical JSON text."""

    name: str = Field(..., min_length=1, max_length=256)
    input: str = Field(..., min_length=2, max_length=MAX_PROBE_INPUT_CHARS)

    @field_validator("input")
    @classmethod
    def _json_object(cls, value: str) -> str:
        if not isinstance(json.loads(value), dict):
            raise ValueError("a probe input is a JSON object")
        return value

    def arguments(self) -> Dict[str, Any]:
        return json.loads(self.input)


class WatchTriageRequest(BaseModel):
    """``watch_triage`` on ``/invocations``: replay this watcher's probe and compare (§4.1).

    Sent on the run's own headless session, with the full run's tool selection fields
    (``enabled_tools``, ``rag_assistant_id``) beside it, so the Runtime resolves the same tools
    the full run would. Optional and additive on ``InvocationRequest``.
    """

    model_config = ConfigDict(populate_by_name=True)

    schedule_id: str = Field(..., alias="scheduleId", min_length=1, max_length=64)
    calls: List[WatchTriageCall] = Field(..., min_length=1, max_length=MAX_PROBE_CALLS)
    baseline: List[str] = Field(..., min_length=1, max_length=MAX_PROBE_CALLS)
    fingerprint_version: int = Field(..., alias="fingerprintVersion")

    @model_validator(mode="after")
    def _one_baseline_per_call(self) -> "WatchTriageRequest":
        if len(self.baseline) != len(self.calls):
            raise ValueError("one baseline fingerprint per call")
        if any(not isinstance(f, str) or len(f) != FINGERPRINT_CHARS for f in self.baseline):
            raise ValueError("a baseline fingerprint is a version-1 digest")
        return self


def skip_reason(state: Any, now: datetime, *, staleness_hours: Optional[int] = None) -> Optional[str]:
    """Why this run goes straight to the full run with no triage (§2.4), or ``None`` to triage.

    ``state`` is the watcher's ``WatchState`` (or ``None``: never run, or started over). The
    flag rows of §2.4 are the caller's: the worker checks its own copy, and the Runtime answers
    ``flag_off`` for its.
    """
    if state is None or not getattr(state, "probe", None):
        return "no_probe"
    if getattr(state, "fingerprint_version", None) != FINGERPRINT_VERSION:
        return "version_mismatch"
    wake_at = _parse_iso(getattr(state, "wake_at", None))
    if wake_at is not None and now >= wake_at:
        return "wake_due"
    last_full = _parse_iso(getattr(state, "last_full_run_at", None))
    hours = staleness_hours if staleness_hours is not None else max_staleness_hours()
    if last_full is None or now - last_full >= timedelta(hours=hours):
        return "stale"
    return None


def triage_request_from_state(schedule_id: str, state: Any) -> Optional[WatchTriageRequest]:
    """The request for the watcher's stored probe, or ``None`` when it can't be sent as one."""
    probe = getattr(state, "probe", None) or []
    try:
        return WatchTriageRequest(
            schedule_id=schedule_id,
            calls=[WatchTriageCall(name=call["name"], input=call["input"]) for call in probe],
            baseline=[call["fingerprint"] for call in probe],
            fingerprint_version=getattr(state, "fingerprint_version", None),
        )
    except Exception:
        return None


@dataclass(frozen=True)
class TriageVerdict:
    """What one triage decided: ``quiet`` or ``escalate``, the tier that decided it, and why."""

    verdict: str
    reason: str
    #: 1 for a verdict tier 1 reached (``unchanged``, ``changed_no_decider``), 2 for the
    #: decider's, ``None`` when the probe never ran to a compare.
    tier: Optional[int] = None
    #: The replayed fingerprints, in call order; ``None`` for a call that produced no result.
    fingerprints: List[Optional[str]] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return self.verdict == QUIET

    def payload(self) -> Dict[str, Any]:
        return {
            "type": WATCH_TRIAGE_EVENT,
            "verdict": self.verdict,
            "tier": self.tier,
            "reason": self.reason,
            "fingerprints": list(self.fingerprints),
        }

    def sse_frame(self) -> str:
        return f"event: {WATCH_TRIAGE_EVENT}\ndata: {json.dumps(self.payload())}\n\n"


def escalate(reason: str, fingerprints: Sequence[Optional[str]] = (), *, tier: Optional[int] = None) -> TriageVerdict:
    return TriageVerdict(verdict=ESCALATE, reason=reason, tier=tier, fingerprints=list(fingerprints))


def compare_fingerprints(baseline: Sequence[str], fingerprints: Sequence[Optional[str]]) -> TriageVerdict:
    """Tier 1: quiet only when every replayed fingerprint equals its baseline, in order."""
    if len(baseline) == len(fingerprints) and all(f is not None and f == b for b, f in zip(baseline, fingerprints)):
        return TriageVerdict(verdict=QUIET, reason="unchanged", tier=1, fingerprints=list(fingerprints))
    # No decider yet (T.3): a change goes to the full run.
    return escalate("changed_no_decider", fingerprints, tier=1)


def verdict_from_frame(payload: Optional[Mapping[str, Any]]) -> TriageVerdict:
    """The worker's reading of a ``watch_triage_result`` frame. Anything it can't trust escalates."""
    if not isinstance(payload, Mapping):
        return escalate(TRIAGE_FAILED)
    verdict, reason, tier = payload.get("verdict"), payload.get("reason"), payload.get("tier")
    if verdict not in VERDICTS or reason not in REASONS or tier not in (None, 1, 2):
        return escalate(TRIAGE_FAILED)
    if verdict == QUIET and reason not in ("unchanged", "decider_quiet"):
        return escalate(TRIAGE_FAILED)
    fingerprints = payload.get("fingerprints")
    if not isinstance(fingerprints, list) or any(f is not None and not isinstance(f, str) for f in fingerprints):
        fingerprints = []
    return TriageVerdict(verdict=verdict, reason=reason, tier=tier, fingerprints=fingerprints)


def history_triage(verdict: Optional[TriageVerdict], *, ms: int, skipped: Optional[str] = None) -> Dict[str, Any]:
    """The ``triage`` map a shadow run's ``SCHEDRUN#`` row carries (§4.2): enums and a duration only."""
    if skipped is not None:
        return {"verdict": "skipped", "reason": skipped}
    assert verdict is not None
    out: Dict[str, Any] = {"verdict": verdict.verdict, "reason": verdict.reason, "ms": int(ms)}
    if verdict.tier is not None:
        out["tier"] = verdict.tier
    return out


__all__ = [
    "DEFAULT_MAX_STALENESS_HOURS",
    "ESCALATE",
    "QUIET",
    "REASONS",
    "REPLAY_TIMEOUT_SECONDS",
    "SKIP_REASONS",
    "TRIAGE_FAILED",
    "TriageVerdict",
    "VERDICTS",
    "WATCH_TRIAGE_EVENT",
    "WatchTriageCall",
    "WatchTriageRequest",
    "compare_fingerprints",
    "escalate",
    "history_triage",
    "max_staleness_hours",
    "skip_reason",
    "triage_request_from_state",
    "verdict_from_frame",
]
