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
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: ``compactionEvents`` kinds that cut the conversation: a new checkpoint
#: (and its forced / floor-unreachable variants) or a truncation anchor that
#: slid forward. ``applied`` is deliberately not one: it is the restore-time
#: slice of an existing checkpoint, recorded on every turn after a cut.
CUT_KINDS = ("checkpoint", "forced", "floor_unreachable", "truncation_anchor")
#: Kinds that took a document out of the context: the post-turn offload, and
#: restore replacing a document with its digest or a contentless placeholder.
OFFLOAD_KINDS = ("document_offload", "document_rehydrated", "document_stripped")
#: The reason a cut or offload carries when it is a truncated tool result
#: rather than a ``compactionEvents`` kind (``applied.truncatedToolResults``).
TOOL_RESULT_TRUNCATION = "tool_results_truncated"


@dataclass(frozen=True)
class DocumentReads:
    calls: int = 0
    pages: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class PlannedFile:
    """One file's entry in a call's ``attachmentPlan`` (unified-attachment-delivery §8.2)."""

    mode: str
    file_class: Optional[str] = None
    tokens: Optional[int] = None
    reason: Optional[str] = None


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
    # Document context (unified-attachment-delivery §8.5). ``None`` means no
    # row carried the field, which is "not tracked", never zero.
    #: Attachment blocks on a prompt, the most any call saw.
    documents_attached: Optional[int] = None
    #: Estimated inline document tokens in context, the most any call carried.
    document_tokens: Optional[int] = None
    #: Digest or placeholder stand-ins in context, the most any call carried.
    document_digests: Optional[int] = None
    #: ``document_read`` calls, pages and bytes, summed over the calls.
    document_reads: Optional[DocumentReads] = None
    #: ``{format: count}`` of the inline attachments, the most of each any call saw.
    document_mime: Tuple[Tuple[str, int], ...] = ()
    #: The first call's ``cacheStatus`` on each turn, in order: turn 2's is the
    #: warm-turn read, a turn after a pause the cold one.
    turn_cache_statuses: Tuple[str, ...] = ()
    #: The delivery decision, from the first call that recorded one.
    attachment_plan: Tuple[PlannedFile, ...] = ()
    #: Cuts and offloads over the sample, by reason (``CUT_KINDS`` / ``OFFLOAD_KINDS``).
    cuts: Tuple[Tuple[str, int], ...] = ()
    offloads: Tuple[Tuple[str, int], ...] = ()

    @property
    def cut_count(self) -> int:
        return sum(n for _, n in self.cuts)

    @property
    def offload_count(self) -> int:
        return sum(n for _, n in self.offloads)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-clean, for a readout file. Ids and numbers only, like the rows it reads."""
        return {
            "calls": self.calls,
            "costUsd": self.cost_usd,
            "cacheStatuses": dict(self.cache_statuses),
            "turnCacheStatuses": list(self.turn_cache_statuses),
            "documentsAttached": self.documents_attached,
            "documentTokens": self.document_tokens,
            "documentDigests": self.document_digests,
            "documentReads": (
                {"calls": self.document_reads.calls, "pages": self.document_reads.pages,
                 "bytes": self.document_reads.bytes}
                if self.document_reads is not None else None
            ),
            "documentMime": dict(self.document_mime),
            "attachmentPlan": [
                {"mode": f.mode, "class": f.file_class, "tokens": f.tokens, "reason": f.reason}
                for f in self.attachment_plan
            ],
            "cuts": dict(self.cuts),
            "offloads": dict(self.offloads),
            "otherModels": list(self.other_models),
            "flags": list(self.flags),
        }

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
        if self.cuts:
            parts.append("cuts " + ", ".join(f"{k} {n}" for k, n in self.cuts))
        if self.offloads:
            parts.append("offloads " + ", ".join(f"{k} {n}" for k, n in self.offloads))
        if self.documents_attached or self.document_tokens or self.document_digests:
            parts.append(
                f"documents {self.documents_attached or 0} attached, ~{self.document_tokens or 0:,} tokens"
                + (f", {self.document_digests} digests" if self.document_digests else "")
            )
        if self.document_reads is not None and self.document_reads.calls:
            parts.append(f"document_read {self.document_reads.calls}× {self.document_reads.pages}p")
        if self.attachment_plan:
            modes = Counter(f.mode for f in self.attachment_plan)
            parts.append("plan " + ", ".join(f"{m} {n}" for m, n in sorted(modes.items())))
        if self.other_models:
            parts.append("ran on " + ", ".join(self.other_models))
        parts.extend(self.flags)
        if self.system_prompt_hash or self.tool_config_hash:
            parts.append(f"sys {self.system_prompt_hash or '—'} tools {self.tool_config_hash or '—'}")
        parts.append(f"${self.cost_usd:.4f}")
        return " · ".join(parts)


def _short(value: Any) -> Optional[str]:
    return str(value)[:6] if value else None


def _num(value: Any) -> Optional[float]:
    """A number from a row field: DynamoDB hands back ``Decimal``; a bool or a string is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def _peak(current: Optional[int], value: Any) -> Optional[int]:
    number = _int(value)
    if number is None:
        return current
    return number if current is None else max(current, number)


def _plan_entries(raw: Any) -> List[PlannedFile]:
    """``attachmentPlan`` as a list of :class:`PlannedFile`, whatever its container.

    The field is new (§8.2) and written by another change, so this accepts a
    list of entries, ``{"files": [...]}``, or ``{upload_id: entry}`` and keeps
    only what a readout needs: never the keys, which are upload ids.
    """
    if isinstance(raw, Mapping):
        raw = raw.get("files") if isinstance(raw.get("files"), list) else list(raw.values())
    if not isinstance(raw, list):
        return []
    entries = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not entry.get("mode"):
            continue
        file_class = entry.get("class", entry.get("fileClass"))
        entries.append(PlannedFile(
            mode=str(entry["mode"]),
            file_class=str(file_class) if file_class else None,
            tokens=_int(entry.get("tokens")),
            reason=str(entry["reason"]) if entry.get("reason") else None,
        ))
    return entries


def summarize_calls(rows: Sequence[Mapping[str, Any]], *, model_id: Optional[str] = None) -> CallSummary:
    """Reduce one session's ``C#`` rows (chronological) to a :class:`CallSummary`.

    Malformed fields count as absent rather than raising: this is a
    diagnostic, and a row written before a field existed is normal.
    """
    statuses: Counter = Counter()
    compaction: Counter = Counter()
    cuts: Counter = Counter()
    offloads: Counter = Counter()
    mime: Dict[str, int] = {}
    models = []
    cost = 0.0
    trimmed = 0
    truncated_tool_results = 0
    attached = tokens = digests = None
    reads: Optional[List[int]] = None
    plan: List[PlannedFile] = []
    turn_statuses: List[str] = []
    turn_open = False
    estimated = agent_switched = model_switched = False
    for row in rows:
        status = str(row.get("cacheStatus") or "unknown")
        statuses[status] += 1
        if not turn_open:
            turn_statuses.append(status)
            turn_open = True
        # The turn's last call alone carries ``turnDurationMs`` (ledger_rollups).
        if row.get("turnDurationMs") is not None:
            turn_open = False
        total = (row.get("cost") or {}).get("total") if isinstance(row.get("cost"), Mapping) else None
        if _num(total) is not None:
            cost += float(_num(total))  # type: ignore[arg-type]
        events = row.get("compactionEvents")
        for event in events if isinstance(events, list) else ():
            if isinstance(event, Mapping) and event.get("kind"):
                kind = str(event["kind"])
                compaction[kind] += 1
                if kind in CUT_KINDS:
                    cuts[kind] += 1
                elif kind in OFFLOAD_KINDS:
                    offloads[kind] += 1
                if kind == "applied":
                    # Recorded on every restore of the same cut: the most, not the sum.
                    truncated_tool_results = max(truncated_tool_results, _int(event.get("truncatedToolResults")) or 0)
        removed = _int(row.get("windowRemovedMessages"))
        if removed is not None:
            trimmed = max(trimmed, removed)
        attached = _peak(attached, row.get("documentsAttached"))
        tokens = _peak(tokens, row.get("documentTokens"))
        digests = _peak(digests, row.get("documentDigests"))
        raw_reads = row.get("documentReads")
        if isinstance(raw_reads, Mapping):
            reads = reads or [0, 0, 0]
            for i, key in enumerate(("calls", "pages", "bytes")):
                reads[i] += _int(raw_reads.get(key)) or 0
        raw_mime = row.get("documentMime")
        if isinstance(raw_mime, Mapping):
            for fmt, count in raw_mime.items():
                mime[str(fmt)] = max(mime.get(str(fmt), 0), _int(count) or 0)
        if not plan:
            plan = _plan_entries(row.get("attachmentPlan"))
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
        documents_attached=attached,
        document_tokens=tokens,
        document_digests=digests,
        document_reads=DocumentReads(*reads) if reads is not None else None,
        document_mime=tuple(sorted(mime.items())),
        turn_cache_statuses=tuple(turn_statuses),
        attachment_plan=tuple(plan),
        cuts=tuple(sorted(cuts.items())),
        offloads=tuple(sorted({**offloads, **({TOOL_RESULT_TRUNCATION: truncated_tool_results}
                                               if truncated_tool_results else {})}.items())),
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
