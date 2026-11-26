"""A quiet watcher's probe: the fingerprint and what a full run leaves for triage.

``docs/specs/quiet-watcher-triage.md`` §3. A full watch run's read-only tool calls are its
**probe**. A later triage (PR T.2) replays them and compares each result's fingerprint with
the one this run's model read. This module is the one definition of that fingerprint, shared
by the Runtime's ``WatchProbeRecorder`` (which fingerprints each result as the run makes it)
and, from T.2, the replay. It also holds the probe's coverage rules (§2.1), the worker's join
of the ``watch_probe`` frame with the run's tool trace, and the ``wakeAt`` clamp (§3.3).

Pure and stdlib only, so the Runtime and the scheduled-runs worker import the same code.
Nothing here does I/O or reads the clock: ``now`` is always passed in.

The fingerprint, version 1 (§3.2):

- JSON (a ``json`` block, or the result's ``structuredContent``) is re-serialized canonically:
  sorted keys, no whitespace.
- Text has its whitespace runs collapsed.
- Any other block (an image, a document) is its canonical JSON with every bytes value replaced
  by the bytes' SHA-256, so a changed picture is a changed fingerprint.
- Blocks are joined in order, and the error flag is included.
- SHA-256, hex, the first 32 characters.

It deliberately strips nothing volatile. Stripping is a per-connector guess about what is noise,
and a wrong guess hides a change, while noise only costs an escalation. Any change to the
normalization bumps :data:`FINGERPRINT_VERSION`, which makes every stored baseline stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: Bumped by any change to the normalization below. A stored probe of another version can't
#: be compared, so its watcher's next run is a full run (§2.4).
FINGERPRINT_VERSION = 1
#: The hex characters kept of each SHA-256.
FINGERPRINT_CHARS = 32
#: A probe of more calls than this is no probe (§2.1).
MAX_PROBE_CALLS = 5
#: The longest call input, in characters of its canonical JSON, a probe keeps (§2.1).
MAX_PROBE_INPUT_CHARS = 2_000
#: A ``wakeAt`` further ahead than this is clamped to it (§3.3).
MAX_WAKE_AHEAD = timedelta(days=30)

#: Why a full run left no probe. Carried on the ``watch_probe`` frame; ids only.
PROBE_REASONS = ("write_tool", "too_many_calls", "input_too_large", "no_calls")

#: The quiet watcher's ``report`` tool, which is never part of a probe. The same string as
#: ``agents.builtin_tools.watch_report_tool.WATCH_REPORT_TOOL_NAME``, which this module can't import.
REPORT_TOOL_NAME = "report"

_WHITESPACE = re.compile(r"\s+")


def _canonical(value: Any) -> str:
    return json.dumps(
        _hash_bytes(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _hash_bytes(value: Any) -> Any:
    """``value`` with every bytes value replaced by its SHA-256, so it serializes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, Mapping):
        return {str(k): _hash_bytes(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_bytes(v) for v in value]
    return value


def _block_text(block: Any) -> str:
    if isinstance(block, Mapping):
        if "text" in block and len(block) == 1:
            return "text:" + _WHITESPACE.sub(" ", str(block["text"])).strip()
        if "json" in block and len(block) == 1:
            return "json:" + _canonical(block["json"])
    return "block:" + _canonical(block)


def fingerprint_result(result: Optional[Mapping[str, Any]]) -> str:
    """The version-1 fingerprint of a Strands tool result (``{status, content, structuredContent?}``).

    Total: a result of any shape, or ``None``, still fingerprints, so the recording hook can
    never fail a run.
    """
    if not isinstance(result, Mapping):
        parts = ["result:" + _canonical(result)]
    else:
        parts = ["error" if result.get("status") == "error" else "ok"]
        content = result.get("content")
        if isinstance(content, (list, tuple)):
            parts.extend(_block_text(block) for block in content)
        elif content is not None:
            parts.append(_block_text(content))
        if result.get("structuredContent") is not None:
            parts.append("structured:" + _canonical(result["structuredContent"]))
    digest = hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_CHARS]


def probe_input_json(tool_input: Any) -> str:
    """A call's input as canonical JSON text: what the probe stores and what its bound counts."""
    return _canonical(tool_input if tool_input is not None else {})


@dataclass(frozen=True)
class ProbeCall:
    """One tool call a full run made, as the recording hook saw it."""

    tool_use_id: str
    name: str
    #: By ``runs_in_parallel``, judged against the tool object the call ran.
    read_only: bool
    fingerprint: str
    input_chars: int


@dataclass(frozen=True)
class ProbeDecision:
    probeable: bool
    reason: Optional[str] = None


def decide_probe(calls: Sequence[ProbeCall]) -> ProbeDecision:
    """Whether a run's calls (``report`` already left out) make a probe, by §2.1's coverage rules.

    A probe covers every call or there is none. A write (anything not known to be read-only)
    is checked first, because a probe runs unattended.
    """
    if not calls:
        return ProbeDecision(False, "no_calls")
    if not all(call.read_only for call in calls):
        return ProbeDecision(False, "write_tool")
    if len(calls) > MAX_PROBE_CALLS:
        return ProbeDecision(False, "too_many_calls")
    if any(call.input_chars > MAX_PROBE_INPUT_CHARS for call in calls):
        return ProbeDecision(False, "input_too_large")
    return ProbeDecision(True)


def watch_probe_payload(calls: Sequence[ProbeCall]) -> Dict[str, Any]:
    """The ``watch_probe`` frame's payload: calls only when probeable, never the inputs."""
    decision = decide_probe(calls)
    payload: Dict[str, Any] = {
        "type": "watch_probe",
        "fingerprintVersion": FINGERPRINT_VERSION,
        "probeable": decision.probeable,
        "calls": (
            [{"toolUseId": c.tool_use_id, "name": c.name, "fingerprint": c.fingerprint} for c in calls]
            if decision.probeable
            else []
        ),
    }
    if decision.reason:
        payload["reason"] = decision.reason
    return payload


def probe_from_run(frame: Optional[Mapping[str, Any]], tool_trace: Iterable[Any]) -> Optional[List[Dict[str, str]]]:
    """The probe to store, from the run's ``watch_probe`` frame and its tool trace, or ``None``.

    The frame names the calls and their fingerprints; the inputs come from the trace (the
    harness's ``ToolTraceEntry``: ``tool_use_id``, ``name``, ``input``), keyed by tool use id.
    Fails closed: no frame, a frame of another version, a frame that isn't probeable, or a
    frame and trace that disagree about which calls the run made, is no probe.
    """
    if not isinstance(frame, Mapping) or frame.get("probeable") is not True:
        return None
    if frame.get("fingerprintVersion") != FINGERPRINT_VERSION:
        return None
    calls = frame.get("calls")
    if not isinstance(calls, list) or not calls or len(calls) > MAX_PROBE_CALLS:
        return None

    if not all(isinstance(c, Mapping) for c in calls):
        return None
    by_id = {c.get("toolUseId"): c for c in calls}
    traced = [entry for entry in tool_trace if getattr(entry, "name", None) != REPORT_TOOL_NAME]
    if len(by_id) != len(calls) or set(by_id) != {getattr(entry, "tool_use_id", "") for entry in traced}:
        # The hook saw a different set of calls than the stream did, so the probe would not
        # cover what the model read.
        return None

    # In the order the model asked for them (the stream's), not the order they finished: a
    # concurrent batch's results can arrive in any order.
    probe: List[Dict[str, str]] = []
    for entry in traced:
        call = by_id[getattr(entry, "tool_use_id", "")]
        fingerprint = call.get("fingerprint")
        name = call.get("name")
        if not isinstance(fingerprint, str) or not fingerprint or not isinstance(name, str) or not name:
            return None
        if name != getattr(entry, "name", None):
            return None
        input_json = probe_input_json(getattr(entry, "input", None))
        if len(input_json) > MAX_PROBE_INPUT_CHARS:
            return None
        probe.append({"name": name, "input": input_json, "fingerprint": fingerprint})
    return probe


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A time with no offset is read as UTC rather than dropped: dropping a wake time
        # loses a full run, reading it as UTC at worst moves it by the user's offset.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def report_wake_ats(tool_trace: Iterable[Any]) -> List[Any]:
    """Every ``state.wakeAt`` the run's ``report`` calls gave, in call order."""
    values: List[Any] = []
    for entry in tool_trace:
        if getattr(entry, "name", None) != REPORT_TOOL_NAME:
            continue
        tool_input = getattr(entry, "input", None)
        state = tool_input.get("state") if isinstance(tool_input, Mapping) else None
        if isinstance(state, Mapping) and state.get("wakeAt") is not None:
            values.append(state["wakeAt"])
    return values


def next_wake_at(values: Iterable[Any], now: datetime) -> Optional[str]:
    """The earliest future ``wakeAt`` given, clamped to :data:`MAX_WAKE_AHEAD`; ``None`` if none.

    A past or unparseable value is dropped (§3.3).
    """
    future = [moment for moment in (_parse_iso(v) for v in values) if moment is not None and moment > now]
    if not future:
        return None
    return iso_utc(min(min(future), now + MAX_WAKE_AHEAD))


@dataclass
class FullRunTriage:
    """What a completed full watch run writes for triage on ``SCHEDSTATE#`` (§3.4).

    ``probe`` ``None`` means the run left no probe, and the stored one is removed with its
    version and run id.
    """

    last_full_run_at: str
    probe: Optional[List[Dict[str, str]]] = None
    probe_run_id: Optional[str] = None
    wake_at: Optional[str] = None
    fingerprint_version: Optional[int] = field(default=None)

    def __post_init__(self) -> None:
        if self.probe is None:
            self.probe_run_id = None
            self.fingerprint_version = None
        elif self.fingerprint_version is None:
            self.fingerprint_version = FINGERPRINT_VERSION


def full_run_triage(
    *,
    frame: Optional[Mapping[str, Any]],
    tool_trace: Sequence[Any],
    run_id: str,
    finished_at: str,
    now: datetime,
) -> FullRunTriage:
    """The triage fields a completed full run leaves (§3.4's first two rows)."""
    probe = probe_from_run(frame, tool_trace)
    return FullRunTriage(
        last_full_run_at=finished_at or iso_utc(now),
        probe=probe,
        probe_run_id=run_id if probe is not None else None,
        wake_at=next_wake_at(report_wake_ats(tool_trace), now),
    )


__all__ = [
    "FINGERPRINT_CHARS",
    "FINGERPRINT_VERSION",
    "MAX_PROBE_CALLS",
    "MAX_PROBE_INPUT_CHARS",
    "MAX_WAKE_AHEAD",
    "PROBE_REASONS",
    "REPORT_TOOL_NAME",
    "FullRunTriage",
    "ProbeCall",
    "ProbeDecision",
    "decide_probe",
    "fingerprint_result",
    "full_run_triage",
    "iso_utc",
    "next_wake_at",
    "probe_from_run",
    "probe_input_json",
    "report_wake_ats",
    "watch_probe_payload",
]
