"""Content-free projections for the admin cost surfaces.

An admin diagnosing a conversation's spend must be able to do it without
reading the conversation. That is a property of the *data path*, not of the
page: if a storage read pulls ``citations[].text`` and the response model just
happens not to render it, the text still crossed the wire, sat in a log
line's ``%r``, and is one refactor away from a column. So the rule is enforced
where the bytes leave DynamoDB — every read behind the per-user conversation
list and the session profile projects an explicit attribute allowlist, and a
test walks both the projections and the response models against the denylist
below.

The denylist is the inventory of every attribute on the ``sessions-metadata``,
``user-file-uploads`` row families that carries user text or model-generated
prose. It is deliberately a list of *paths*, not a heuristic: a new attribute
is content-bearing when someone says so here, and the test fails until they do.

Two readers deliberately read one denylisted attribute — ``compaction.summary``
— because its **length** is a diagnostic (a summary that is 40% of the window
guarantees the compaction spiral, #833 D2). They measure it and drop the string
before returning, the same discipline ``scripts/scan_fleet_prefix_spend.py``
uses. The contract the test enforces is therefore on what the reader *returns*,
not only on what it requests: no dict that leaves these readers contains a
content-bearing path.

Lives in ``apis.shared`` because both the storage layer and the admin service
need it, and ``apis.shared`` may import neither.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Denylist — attribute paths that carry user text or model-generated prose.
# A path denies itself and everything beneath it (``pausedTurn`` denies
# ``pausedTurn.modelId``); a dotted path denies only that leaf
# (``compaction.summary`` leaves ``compaction.checkpoint`` alone).
# ---------------------------------------------------------------------------
CONTENT_BEARING: FrozenSet[str] = frozenset({
    # S# session rows
    "title",                          # LLM-generated from the first user message
    "tags",                           # user-typed
    "preferences.customPromptText",   # user-authored system prompt
    "compaction.summary",             # model-generated conversation summary
    "pendingInterrupts",              # tool_input + prompt text
    "pausedTurn",                     # turn-construction snapshot, may carry prompt context
    "exportReceipts",                 # fileName + webViewLink
    # C# per-call cost rows
    "citations",                      # text (≤500 chars of a KB document) + fileName
    "displayText",                    # the user's original message
    # TSUM# / APPCARD# / UIRES# / LEASE# / SCHEDPROMPT# / RUN#
    "summary",                        # tool-batch prose
    "content",                        # tool-result blocks
    "arguments",                      # tool arguments
    "htmlGz",                         # model-produced App HTML
    "steerQueue",                     # mid-turn follow-up text
    "promptText",
    "label",
    "stateReason",
    "lastError",
    "errorDetail",
    # FILE# upload rows
    "filename",                       # user-chosen
    "s3Key",                          # embeds the filename
    "s3Uri",
    "digest.abstract",                # model-generated abstract of the document
    "digest.sections",                # heading text lifted from the document
})

#: The one path a content-free reader may *request* but must never *return*.
MEASURED_ONLY: FrozenSet[str] = frozenset({"compaction.summary"})


def is_content_bearing(path: str) -> bool:
    """True if any contiguous run of ``path``'s segments is a denylisted path.

    Rows get embedded under arbitrary keys on the way out (``calls[]`` in the
    anatomy, ``sessions[]`` in a list response), so ``calls.citations.text``
    must be caught by the ``citations`` entry and ``sessions.title`` by
    ``title``. A denied path also denies everything beneath it
    (``pausedTurn.modelId``), while a dotted entry denies only that leaf
    (``compaction.summary`` leaves ``compaction.checkpoint`` alone).
    """
    parts = path.split(".")
    for start in range(len(parts)):
        for end in range(start + 1, len(parts) + 1):
            if ".".join(parts[start:end]) in CONTENT_BEARING:
                return True
    return False


# ---------------------------------------------------------------------------
# Allowlisted projections. Tuples of attribute paths; ``build_projection``
# turns them into a DynamoDB ProjectionExpression with every segment aliased,
# so reserved words (``status``, ``timestamp``, ``cost``, ``source``) never
# need case-by-case handling.
# ---------------------------------------------------------------------------

#: S# rows for the per-user conversation list and the session profile.
SESSION_ROW_PROJECTION: Tuple[str, ...] = (
    "sessionId",
    "userId",
    "status",
    "deleted",
    "createdAt",
    "lastMessageAt",
    "messageCount",
    "totalCost",
    "lastContextTokens",
    "contextWindow",
    "totalCacheReadTokens",
    "totalCacheWriteTokens",
    "avoidableMissCount",
    "partialMissCount",
    "wastedUsd",
    "partialMissUsd",
    "preferences.lastModel",
    "preferences.enabledTools",
    "preferences.assistantId",
    "preferences.agentType",
    "compaction.checkpoint",
    "compaction.truncationAnchor",
    "compaction.totalSummarizedTurns",
    "compaction.lastInputTokens",
    "compaction.updatedAt",
    "compaction.summary",   # MEASURED_ONLY — length reported as summaryChars, string dropped
    # Behavioral counters (optional; absent on rows written before they shipped)
    "toolCallCount",
    "toolErrorCount",
    "compactionCount",
    # Compaction decisions by kind (per-call ledger rolled up; see
    # `TurnBasedSessionManager.record_compaction_event`)
    "compactionAppliedCount",
    "compactionForcedCount",
    "compactionFloorUnreachableCount",
    # Message-feedback rollups (live F# rows written while diagnostics were
    # on; see `apis.shared.sessions.feedback`)
    "thumbsUp",
    "thumbsDown",
    # Document lifecycle rollups (per-call document fields summed; see
    # `apis.shared.sessions.metadata.DOCUMENT_ROLLUP_ATTRS`)
    "fullDocumentCalls",
    "digestOnlyCalls",
    "documentReadCalls",
    "documentReadPages",
)

#: C# rows for the cost anatomy and the session profile's trajectory.
CALL_ROW_PROJECTION: Tuple[str, ...] = (
    "sessionId",
    "messageId",
    "timestamp",
    "modelInfo.modelId",
    "tokenUsage",
    "cost",
    "cacheStatus",
    "cacheGapSeconds",
    "cachePrefixGapSeconds",
    "wastedUsd",
    "agentSwitched",
    "turnAgentId",
    "prefixFingerprints",
    "contextWindow",
    "toolCalls",            # per-call census, optional (PR-3)
    # Context ledger, optional: the agent's stable prefix split
    # ({system, tools} tokens), the conversation window's cumulative trim
    # count, and the compaction decisions taken before this call — all
    # numbers, never text.
    "prefixTokens",
    "windowRemovedMessages",
    "compactionEvents",
    # Document context, optional: the attachment footprint of the live
    # context at this call (counts, estimated tokens, a format→count map keyed
    # by Bedrock's format enum) and the document_read retrievals the call
    # requested. Numbers and enum keys only — never a filename or a byte.
    "hasDocuments",
    "documentCount",
    "documentTokens",
    "documentDigests",
    "documentsAttached",
    "documentSlices",
    "documentSliceTokens",
    "documentMime",
    "documentReads",
)

#: FILE# rows for the session profile's attachment summary.
FILE_ROW_PROJECTION: Tuple[str, ...] = (
    "uploadId",
    "sessionId",
    "sizeBytes",
    "mimeType",
    "source",
    "status",
    "createdAt",
    # DocumentDigest coverage (numbers and the format enum only; the
    # abstract and section titles are denylisted above).
    "digest.status",
    "digest.format",
    "digest.count",
    "digest.tokens",
)

#: F# rows for the session profile's feedback join. A thumb is a ±1, an
#: optional reason *code*, a `signal` discriminator (explicit / implicit,
#: response-feedback spec §10) and a timestamp — never text, by the request
#: model's closed enum (`apis.shared.sessions.models.FEEDBACK_REASONS`).
FEEDBACK_ROW_PROJECTION: Tuple[str, ...] = (
    "sessionId",
    "messageId",
    "value",
    "reason",
    "signal",
    "retryMessageId",   # a message index, the retry-with-correction link
    "updatedAt",
)

ALL_PROJECTIONS: Dict[str, Tuple[str, ...]] = {
    "SESSION_ROW_PROJECTION": SESSION_ROW_PROJECTION,
    "CALL_ROW_PROJECTION": CALL_ROW_PROJECTION,
    "FILE_ROW_PROJECTION": FILE_ROW_PROJECTION,
    "FEEDBACK_ROW_PROJECTION": FEEDBACK_ROW_PROJECTION,
}


def build_projection(paths: Sequence[str]) -> Tuple[str, Dict[str, str]]:
    """Render ``paths`` as ``(ProjectionExpression, ExpressionAttributeNames)``.

    Every path segment is aliased (``#p0.#p1``) so the expression is valid
    regardless of DynamoDB reserved words. Aliases are shared across paths that
    reuse a segment (``preferences.lastModel`` and ``preferences.enabledTools``
    share ``#preferences``), keeping the names map small and deterministic.
    """
    names: Dict[str, str] = {}
    aliases: Dict[str, str] = {}
    rendered: List[str] = []
    for path in paths:
        parts = []
        for segment in path.split("."):
            alias = aliases.get(segment)
            if alias is None:
                alias = f"#cf{len(aliases)}"
                aliases[segment] = alias
                names[alias] = segment
            parts.append(alias)
        rendered.append(".".join(parts))
    return ", ".join(rendered), names


def content_bearing_paths(obj: Any, prefix: str = "") -> List[str]:
    """Walk a dict/list payload and return every content-bearing path found.

    Used by the test suite against both raw storage results and serialized
    response models, and by the readers' own post-processing as a belt-and-
    braces check in debug builds. Lists are walked without indexing so a
    finding reads as ``citations`` rather than ``citations.3``.
    """
    found: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_content_bearing(path):
                found.append(path)
                continue
            found.extend(content_bearing_paths(value, path))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(content_bearing_paths(value, prefix))
    return found


def strip_content(obj: Any, prefix: str = "") -> Any:
    """Return a deep copy of ``obj`` with every content-bearing path removed.

    Defensive final step for the content-free readers: even if a projection is
    widened by mistake, nothing denylisted leaves the storage layer.
    """
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_content_bearing(path):
                continue
            out[key] = strip_content(value, path)
        return out
    if isinstance(obj, list):
        return [strip_content(value, prefix) for value in obj]
    return obj


def measure_compaction_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Replace ``compaction.summary`` with its length as ``compaction.summaryChars``.

    Mutates and returns ``item``. A missing or non-string summary yields no
    ``summaryChars`` key, so a reader can tell "never compacted" from "empty".
    """
    compaction = item.get("compaction")
    if isinstance(compaction, dict):
        summary = compaction.pop("summary", None)
        if isinstance(summary, str):
            compaction["summaryChars"] = len(summary)
    return item


def allowlisted_keys(paths: Iterable[str], parent: str) -> FrozenSet[str]:
    """The leaf names allowlisted directly under ``parent`` (e.g. ``preferences``)."""
    prefix = parent + "."
    return frozenset(p[len(prefix):] for p in paths if p.startswith(prefix) and "." not in p[len(prefix):])
