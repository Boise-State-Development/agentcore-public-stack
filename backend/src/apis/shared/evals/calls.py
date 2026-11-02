"""What the model calls behind a sample looked like, from the session's ``C#`` rows.

``score --explain`` prints a flipped case's answers and tool calls; this adds
the one thing neither shows: what the platform did to the context on those
calls. A lost retention case whose failing sample shows ``compaction applied``
is a compaction question, not a model question; a sample whose calls ran on a
different model than the run asked for is a fallback, not a regression; a
system-prompt hash that differs between a passing and a failing sample of the
same case says the prompt moved under the run (spec §14, open question 3).

The rows come from ``DynamoDBStorage.get_session_cost_records``, the same
content-free reader the admin cost anatomy uses, so nothing here can see
message text. They survive the run's own cleanup: deleting a session leaves a
tombstone and keeps its cost rows. Everything is best-effort — a missing row
or a failed read prints as such and never changes ``score``'s exit code.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class CallSummary:
    """One session's model calls, reduced to what explains a flip."""

    calls: int
    cost_usd: float
    cache_statuses: Tuple[Tuple[str, int], ...] = ()
    compaction_events: Tuple[Tuple[str, int], ...] = ()
    trimmed_messages: int = 0
    #: Model ids the calls ran on that differ from the one the run asked for.
    other_models: Tuple[str, ...] = ()
    #: "usage estimated" / "agent switched" / "model switched", when any call says so.
    flags: Tuple[str, ...] = ()
    #: The first call's prefix fingerprints, shortened: comparable across samples.
    system_prompt_hash: Optional[str] = None
    tool_config_hash: Optional[str] = None

    def line(self) -> str:
        if not self.calls:
            return "no cost rows"
        parts = [f"{self.calls} call{'s' if self.calls != 1 else ''}"]
        if self.cache_statuses:
            parts.append("cache " + ", ".join(f"{s} {n}" for s, n in self.cache_statuses))
        if self.compaction_events:
            parts.append("compaction " + ", ".join(f"{k} {n}" for k, n in self.compaction_events))
        if self.trimmed_messages:
            parts.append(f"window trimmed {self.trimmed_messages}")
        if self.other_models:
            parts.append("ran on " + ", ".join(self.other_models))
        parts.extend(self.flags)
        if self.system_prompt_hash or self.tool_config_hash:
            parts.append(f"sys {self.system_prompt_hash or '—'} tools {self.tool_config_hash or '—'}")
        parts.append(f"${self.cost_usd:.4f}")
        return " · ".join(parts)


def _short(value: Any) -> Optional[str]:
    return str(value)[:6] if value else None


def summarize_calls(rows: Sequence[Mapping[str, Any]], *, model_id: Optional[str] = None) -> CallSummary:
    """Reduce one session's ``C#`` rows (chronological) to a :class:`CallSummary`.

    Malformed fields count as absent rather than raising: this is a
    diagnostic, and a row written before a field existed is normal.
    """
    statuses: Counter = Counter()
    compaction: Counter = Counter()
    models = []
    cost = 0.0
    trimmed = 0
    estimated = agent_switched = model_switched = False
    for row in rows:
        statuses[str(row.get("cacheStatus") or "unknown")] += 1
        total = (row.get("cost") or {}).get("total") if isinstance(row.get("cost"), Mapping) else None
        if isinstance(total, (int, float)):
            cost += float(total)
        for event in row.get("compactionEvents") or ():
            if isinstance(event, Mapping) and event.get("kind"):
                compaction[str(event["kind"])] += 1
        removed = row.get("windowRemovedMessages")
        if isinstance(removed, (int, float)):
            trimmed = max(trimmed, int(removed))
        call_model = (row.get("modelInfo") or {}).get("modelId") if isinstance(row.get("modelInfo"), Mapping) else None
        if call_model and call_model != model_id and call_model not in models:
            models.append(str(call_model))
        estimated = estimated or bool(row.get("usageEstimated"))
        agent_switched = agent_switched or bool(row.get("agentSwitched"))
        model_switched = model_switched or bool(row.get("modelSwitched"))

    first = rows[0].get("prefixFingerprints") if rows else None
    first = first if isinstance(first, Mapping) else {}
    flags = tuple(
        name
        for name, on in (
            ("usage estimated", estimated),
            ("agent switched", agent_switched),
            ("model switched", model_switched),
        )
        if on
    )
    return CallSummary(
        calls=len(rows),
        cost_usd=round(cost, 6),
        cache_statuses=tuple(sorted(statuses.items())),
        compaction_events=tuple(sorted(compaction.items())),
        trimmed_messages=trimmed,
        other_models=tuple(models),
        flags=flags,
        system_prompt_hash=_short(first.get("systemPromptHash")),
        tool_config_hash=_short(first.get("toolConfigHash")),
    )


async def fetch_session_calls(session_ids: Iterable[str], storage: Any) -> Dict[str, Sequence[Mapping[str, Any]]]:
    """``{session_id: rows}`` through ``storage.get_session_cost_records``.

    A session whose read fails is left out, so the caller prints "unavailable"
    for it rather than a misleading "no cost rows".
    """
    found: Dict[str, Sequence[Mapping[str, Any]]] = {}
    for session_id in dict.fromkeys(s for s in session_ids if s):
        try:
            found[session_id] = await storage.get_session_cost_records(session_id)
        except Exception:  # noqa: BLE001 — a diagnostic never aborts score
            continue
    return found
