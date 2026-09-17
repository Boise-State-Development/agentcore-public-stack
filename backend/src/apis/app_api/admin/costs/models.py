"""Admin cost dashboard Pydantic models.

These models define the API response schemas for the admin cost dashboard,
enabling administrators to view system-wide usage metrics, top users by cost,
and cost trends.
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Optional, List, Dict


class TopUserCost(BaseModel):
    """User cost summary for admin dashboard top users list."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    last_updated: str = Field(..., alias="lastUpdated")

    # Optional enrichment fields
    email: Optional[str] = None
    tier_name: Optional[str] = Field(None, alias="tierName")
    quota_limit: Optional[float] = Field(None, alias="quotaLimit")
    quota_percentage: Optional[float] = Field(None, alias="quotaPercentage")


class ModelBreakdownItem(BaseModel):
    """Model breakdown item within system cost summary."""
    model_config = ConfigDict(populate_by_name=True)

    cost: float
    requests: int


class SystemCostSummary(BaseModel):
    """System-wide cost summary for a period."""
    model_config = ConfigDict(populate_by_name=True)

    period: str  # "2025-01" or "2025-01-15"
    period_type: str = Field(..., alias="periodType")  # "daily" or "monthly"

    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    active_users: int = Field(..., alias="activeUsers")

    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")
    total_cache_savings: float = Field(0.0, alias="totalCacheSavings")

    model_breakdown: Optional[Dict[str, ModelBreakdownItem]] = Field(
        None,
        alias="modelBreakdown"
    )
    last_updated: str = Field(..., alias="lastUpdated")


class ModelUsageSummary(BaseModel):
    """Per-model usage summary for analytics."""
    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider: str

    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    unique_users: int = Field(..., alias="uniqueUsers")
    avg_cost_per_request: float = Field(..., alias="avgCostPerRequest")

    total_input_tokens: int = Field(..., alias="totalInputTokens")
    total_output_tokens: int = Field(..., alias="totalOutputTokens")


class TierUsageSummary(BaseModel):
    """Per-tier usage summary for quota tier analytics."""
    model_config = ConfigDict(populate_by_name=True)

    tier_id: str = Field(..., alias="tierId")
    tier_name: str = Field(..., alias="tierName")

    total_cost: float = Field(..., alias="totalCost")
    total_users: int = Field(..., alias="totalUsers")
    users_at_limit: int = Field(..., alias="usersAtLimit")
    users_warned: int = Field(..., alias="usersWarned")
    avg_utilization: float = Field(..., alias="avgUtilization")


class CostTrend(BaseModel):
    """Cost trend data point for time-series charts."""
    model_config = ConfigDict(populate_by_name=True)

    date: str
    total_cost: float = Field(..., alias="totalCost")
    total_requests: int = Field(..., alias="totalRequests")
    active_users: int = Field(..., alias="activeUsers")


class PrefixFingerprints(BaseModel):
    """Prompt-cache prefix hashes for one model call (see PrefixFingerprintHook)."""
    model_config = ConfigDict(populate_by_name=True)

    tool_config_hash: Optional[str] = Field(None, alias="toolConfigHash")
    system_prompt_hash: Optional[str] = Field(None, alias="systemPromptHash")
    history_hash: Optional[str] = Field(None, alias="historyHash")
    message_count: Optional[int] = Field(None, alias="messageCount")


class PrefixTokens(BaseModel):
    """The agent's stable static prefix, split: system prompt vs tool schemas."""
    model_config = ConfigDict(populate_by_name=True)

    system: int = 0
    tools: int = 0


class CompactionEvent(BaseModel):
    """One compaction decision recorded before a model call (numbers only).

    ``kind``: ``applied`` (restore-time slice ran), ``checkpoint`` (a new
    checkpoint was cut after the previous turn), ``forced`` / ``floor_unreachable``
    (reserved for the scheduling policy). ``summaryTokens`` is the summary's
    size at that moment — the number a summary cap has to move.
    """
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    kind: str
    checkpoint: Optional[int] = None
    summary_tokens: Optional[int] = Field(None, alias="summaryTokens")
    summarized_turns: Optional[int] = Field(None, alias="summarizedTurns")
    retained_messages: Optional[int] = Field(None, alias="retainedMessages")
    truncated_tool_results: Optional[int] = Field(None, alias="truncatedToolResults")
    input_tokens: Optional[int] = Field(None, alias="inputTokens")
    # Document lifecycle kinds (`document_stripped` / `document_rehydrated` /
    # `document_offload`): how many documents the event touched, their
    # estimated token weight, and — for an offload — the prompt-cache gap at
    # the moment it fired (an offload while the cache is live is the
    # regression the trigger must never produce).
    documents: Optional[int] = None
    document_tokens: Optional[int] = Field(None, alias="documentTokens")
    cache_gap_seconds: Optional[int] = Field(None, alias="cacheGapSeconds")
    # `document_offload` only: what the evicted documents were replaced with,
    # and how many document_read page slices were aged in the same pass.
    digest_tokens: Optional[int] = Field(None, alias="digestTokens")
    slices: Optional[int] = None
    slice_tokens: Optional[int] = Field(None, alias="sliceTokens")


class DocumentReads(BaseModel):
    """``document_read`` retrievals one model call requested: calls, pages
    returned as native document blocks, and their byte size."""
    model_config = ConfigDict(populate_by_name=True)

    calls: int = 0
    pages: int = 0
    bytes: int = 0


class SessionCallRow(BaseModel):
    """One model call within a session's cost anatomy."""
    model_config = ConfigDict(populate_by_name=True)

    timestamp: str
    message_id: Optional[int] = Field(None, alias="messageId")
    model_id: Optional[str] = Field(None, alias="modelId")

    input_tokens: int = Field(0, alias="inputTokens")
    output_tokens: int = Field(0, alias="outputTokens")
    cache_read_tokens: int = Field(0, alias="cacheReadTokens")
    cache_write_tokens: int = Field(0, alias="cacheWriteTokens")

    cost: float = 0.0
    cache_status: Optional[str] = Field(None, alias="cacheStatus")
    cache_gap_seconds: Optional[int] = Field(None, alias="cacheGapSeconds")
    # Seconds since the last call with the SAME prefix, present only when that
    # was an older call than the immediately previous one (#753). Its absence
    # means the two coincide; its presence explains a status that would
    # otherwise look inconsistent with `cacheGapSeconds`.
    cache_prefix_gap_seconds: Optional[int] = Field(
        None, alias="cachePrefixGapSeconds"
    )
    wasted_usd: float = Field(0.0, alias="wastedUsd")
    # #756 — which Agent ran this call, and whether that changed from the call
    # before it. An `@`-mention (Marketplace D11) hands one turn to a different
    # Agent, which genuinely re-writes the prefix; without this a deliberate swap
    # is indistinguishable from a nondeterministic-ordering regression, since both
    # flip `toolConfigHash` and `systemPromptHash` together.
    turn_agent_id: Optional[str] = Field(None, alias="turnAgentId")
    agent_switched: bool = Field(
        False,
        alias="agentSwitched",
        description="This call ran on a different Agent than the previous one — an explained prefix re-write",
    )
    prefix_fingerprints: Optional[PrefixFingerprints] = Field(
        None, alias="prefixFingerprints"
    )
    # Context ledger (optional; absent on rows written before it shipped or
    # while COST_DIAGNOSTICS_ENABLED=false).
    prefix_tokens: Optional[PrefixTokens] = Field(None, alias="prefixTokens")
    # The conversation window's cumulative trimmed-message count at this call.
    window_removed_messages: Optional[int] = Field(None, alias="windowRemovedMessages")
    # Messages trimmed since the previous ledger-bearing call — derived, so a
    # reader does not have to diff consecutive rows. A positive value means
    # the prefix changed before this call.
    window_trimmed: Optional[int] = Field(None, alias="windowTrimmed")
    compaction_events: Optional[List[CompactionEvent]] = Field(None, alias="compactionEvents")
    # Document context at this call (optional; absent on rows written before
    # it shipped or with diagnostics off). `hasDocuments` + `documentDigests`
    # classify the call: full document inline, digest only, or neither.
    # `documentTokens` is the compaction estimator's heuristic (bytes/4, flat
    # per image), comparable across rows; `documentMime` is keyed by Bedrock's
    # format enum plus `image` — never a filename.
    has_documents: Optional[bool] = Field(None, alias="hasDocuments")
    document_count: Optional[int] = Field(None, alias="documentCount")
    document_tokens: Optional[int] = Field(None, alias="documentTokens")
    document_digests: Optional[int] = Field(None, alias="documentDigests")
    documents_attached: Optional[int] = Field(None, alias="documentsAttached")
    document_slices: Optional[int] = Field(None, alias="documentSlices")
    document_slice_tokens: Optional[int] = Field(None, alias="documentSliceTokens")
    document_mime: Optional[Dict[str, int]] = Field(None, alias="documentMime")
    document_reads: Optional[DocumentReads] = Field(None, alias="documentReads")


class SessionCostAnatomy(BaseModel):
    """Per-call cost anatomy for one session (admin cache-miss forensics)."""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    calls: List[SessionCallRow]

    total_cost: float = Field(0.0, alias="totalCost")
    total_cache_read_tokens: int = Field(0, alias="totalCacheReadTokens")
    total_cache_write_tokens: int = Field(0, alias="totalCacheWriteTokens")
    avoidable_miss_count: int = Field(0, alias="avoidableMissCount")
    # Calls that read a leading segment and re-wrote the rest of the prefix
    # against a live cache entry — the shape that hid inside `hit` until the
    # 2026-08-05 compaction spiral. `partialMissUsd` is a subset of
    # `wastedUsd` (same split discipline as the agent-switch fields below).
    partial_miss_count: int = Field(0, alias="partialMissCount")
    partial_miss_usd: float = Field(0.0, alias="partialMissUsd")
    wasted_usd: float = Field(0.0, alias="wastedUsd")
    # #756 — the subset of the two figures above that an Agent switch explains.
    # Deliberately a *split*, not a deduction: the totals still carry every dollar
    # spent, because hiding the cost of `@`-mentions would understate a feature we
    # want to be able to measure on purpose. Subtract to get unexplained waste,
    # which is the number a prefix-stability regression moves.
    agent_switch_miss_count: int = Field(0, alias="agentSwitchMissCount")
    agent_switch_usd: float = Field(0.0, alias="agentSwitchUsd")
    # cacheRead / (cacheRead + cacheWrite) over the session; None until
    # there has been any cache activity.
    cache_efficiency: Optional[float] = Field(None, alias="cacheEfficiency")


class TopSessionCost(BaseModel):
    """One conversation in the "most expensive sessions" list.

    ``totalCost`` is the session's **lifetime** cost — the denormalized
    aggregate ``_bump_session_aggregates`` maintains on the session row —
    not its cost within the requested period. A runaway conversation is
    usually a single long thread that spans period boundaries (the incident
    session opened 2026-07-30 and blocked a quota on 2026-08-04), so
    lifetime is the number support wants; ``period`` scopes *which* sessions
    are listed (those active in it), not how their cost is summed.
    """
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    user_id: str = Field(..., alias="userId")
    title: Optional[str] = None
    total_cost: float = Field(..., alias="totalCost")
    last_message_at: Optional[str] = Field(None, alias="lastMessageAt")
    created_at: Optional[str] = Field(None, alias="createdAt")
    message_count: Optional[int] = Field(None, alias="messageCount")
    last_context_tokens: Optional[int] = Field(None, alias="lastContextTokens")
    # Per-session prompt-cache rollups, when present — a runaway session with
    # a high partialMissUsd is a platform bug, not a heavy user.
    partial_miss_count: Optional[int] = Field(None, alias="partialMissCount")
    partial_miss_usd: Optional[float] = Field(None, alias="partialMissUsd")
    # The user's share for the period, so a support view can say "this one
    # conversation is 90% of their month" without a second call.
    user_period_cost: Optional[float] = Field(None, alias="userPeriodCost")
    share_of_user_period: Optional[float] = Field(None, alias="shareOfUserPeriod")


class TopSessionsResponse(BaseModel):
    """Most expensive conversations for a period, cost-sorted."""
    model_config = ConfigDict(populate_by_name=True)

    period: str
    sessions: List[TopSessionCost]
    # How the list was assembled, stated rather than implied: the scan starts
    # from the period's top-cost users, so a session belonging to a user
    # outside `usersScanned` is not in this list.
    users_scanned: int = Field(0, alias="usersScanned")
    truncated: bool = Field(
        False,
        description="True when more users had period cost than were scanned",
    )


class AdminCostDashboard(BaseModel):
    """Complete admin cost dashboard response combining all metrics."""
    model_config = ConfigDict(populate_by_name=True)

    # Current period summary
    current_period: SystemCostSummary = Field(..., alias="currentPeriod")

    # Top users (configurable limit, default 100)
    top_users: List[TopUserCost] = Field(..., alias="topUsers")

    # Model breakdown
    model_usage: List[ModelUsageSummary] = Field(..., alias="modelUsage")

    # Tier breakdown (optional, if quota system enabled)
    tier_usage: Optional[List[TierUsageSummary]] = Field(None, alias="tierUsage")

    # Historical daily trends (optional)
    daily_trends: Optional[List[CostTrend]] = Field(None, alias="dailyTrends")


# =============================================================================
# Per-user conversation list + session profile (content-free drill-down)
#
# Every field below is a count, a token figure, a dollar figure, a timestamp,
# a hash-derived count, or a catalog/tool *id*. No field may carry user text
# or model-generated prose — `tests/.../test_content_policy.py` walks these
# models against `apis.shared.observability.content_policy.CONTENT_BEARING`.
# The single exemption on this surface is `TopSessionCost.title`, above.
# =============================================================================


class SessionDiagnosis(BaseModel):
    """One named finding from `diagnoses.run_diagnoses`."""
    model_config = ConfigDict(populate_by_name=True)

    code: str
    severity: str  # high | warn | info
    headline: str
    evidence: Dict[str, Any] = Field(default_factory=dict)
    suggestion: str
    ref: str


class UserSessionSummary(BaseModel):
    """One conversation in a user's content-free conversation list.

    ``costKnown=False`` means the session's cost aggregate was never written —
    the cost is *unrecorded*, not zero — and ``totalCost`` is then ``None``.
    Optional counters (``toolCallCount``, ``toolErrorCount``,
    ``compactionCount``) are ``None`` on rows written before they shipped, so
    a UI can say "not tracked" rather than "0".
    """
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    created_at: Optional[str] = Field(None, alias="createdAt")
    last_message_at: Optional[str] = Field(None, alias="lastMessageAt")
    status: Optional[str] = None
    message_count: Optional[int] = Field(None, alias="messageCount")

    model_id: Optional[str] = Field(None, alias="modelId")
    enabled_tool_count: Optional[int] = Field(None, alias="enabledToolCount")
    agent_bound: bool = Field(False, alias="agentBound")

    last_context_tokens: Optional[int] = Field(None, alias="lastContextTokens")
    context_window: Optional[int] = Field(None, alias="contextWindow")
    # lastContextTokens / contextWindow, 0..1, when both are known
    context_share: Optional[float] = Field(None, alias="contextShare")

    total_cost: Optional[float] = Field(None, alias="totalCost")
    cost_known: bool = Field(False, alias="costKnown")
    share_of_user_period: Optional[float] = Field(None, alias="shareOfUserPeriod")

    cache_efficiency: Optional[float] = Field(None, alias="cacheEfficiency")
    wasted_usd: Optional[float] = Field(None, alias="wastedUsd")
    partial_miss_usd: Optional[float] = Field(None, alias="partialMissUsd")

    summarized_turns: Optional[int] = Field(None, alias="summarizedTurns")
    summary_approx_tokens: Optional[int] = Field(None, alias="summaryApproxTokens")

    tool_call_count: Optional[int] = Field(None, alias="toolCallCount")
    tool_error_count: Optional[int] = Field(None, alias="toolErrorCount")
    compaction_count: Optional[int] = Field(None, alias="compactionCount")
    compaction_applied_count: Optional[int] = Field(None, alias="compactionAppliedCount")
    compaction_forced_count: Optional[int] = Field(None, alias="compactionForcedCount")
    compaction_floor_unreachable_count: Optional[int] = Field(
        None, alias="compactionFloorUnreachableCount"
    )

    diagnosis_count: int = Field(0, alias="diagnosisCount")
    top_diagnosis_severity: Optional[str] = Field(None, alias="topDiagnosisSeverity")


class UserSessionsResponse(BaseModel):
    """A user's conversations, content-free, for the admin user page."""
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId")
    # The period the list was scoped to (YYYY-MM), or None for all time.
    period: Optional[str] = None
    # The user's recorded cost for `period`, when scoped; the denominator of
    # each row's `shareOfUserPeriod`.
    user_period_cost: Optional[float] = Field(None, alias="userPeriodCost")
    sessions: List[UserSessionSummary]
    # Rows before `limit` was applied, so the page can say "showing 50 of 212".
    total: int = 0
    # Sessions excluded because their cost is unrecorded (they are still listed,
    # with costKnown=False, when sort != "cost"; under cost-sort they trail).
    unknown_cost_count: int = Field(0, alias="unknownCostCount")
    # Soft-deleted conversations in the list (status="deleted"). Listed, not
    # hidden: a delete removes the row from the user's sidebar, not its cost
    # rows or its share of `userPeriodCost`, so an audit that dropped them
    # could not account for the period total.
    deleted_session_count: int = Field(0, alias="deletedSessionCount")
    deleted_session_cost: float = Field(0.0, alias="deletedSessionCost")


class AttachmentProfile(BaseModel):
    """Per-session upload stats. Never filenames."""
    model_config = ConfigDict(populate_by_name=True)

    count: int = 0
    total_bytes: int = Field(0, alias="totalBytes")
    by_mime: Dict[str, int] = Field(default_factory=dict, alias="byMime")
    # DocumentDigest coverage: uploads with a ready digest and the rendered
    # token estimate they would cost in context (offload spec §4A).
    digested: int = 0
    digest_tokens: int = Field(0, alias="digestTokens")


class ContextTrajectoryPoint(BaseModel):
    """One model call's context occupancy (input + cacheRead + cacheWrite)."""
    model_config = ConfigDict(populate_by_name=True)

    call_index: int = Field(..., alias="callIndex")
    timestamp: str
    context_tokens: int = Field(..., alias="contextTokens")
    cache_status: Optional[str] = Field(None, alias="cacheStatus")
    model_id: Optional[str] = Field(None, alias="modelId")
    cost: Optional[float] = None
    # Per-call tool census when recorded (PR-3): tool name -> calls
    tool_calls: Optional[Dict[str, int]] = Field(None, alias="toolCalls")
    # Context ledger when recorded: messages trimmed before this call, and the
    # kinds of compaction decision taken before it.
    window_trimmed: Optional[int] = Field(None, alias="windowTrimmed")
    compaction: Optional[List[str]] = None


class FingerprintChanges(BaseModel):
    """How often each cached-prefix component changed between consecutive calls."""
    model_config = ConfigDict(populate_by_name=True)

    system_prompt: int = Field(0, alias="systemPrompt")
    tool_config: int = Field(0, alias="toolConfig")
    # The subset of the two figures above that an Agent switch explains.
    explained_by_agent_switch: int = Field(0, alias="explainedByAgentSwitch")


class ToolCensusEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    calls: int = 0
    errors: int = 0


class FeedbackCounts(BaseModel):
    """Thumbs on one bucket of calls: ``up`` / ``down`` are counts of live
    feedback rows, never the text of anything."""
    model_config = ConfigDict(populate_by_name=True)

    up: int = 0
    down: int = 0


class FeedbackByTurnClass(BaseModel):
    """Feedback split by the call's document turn class (document-context
    offload spec §6.1): *full* (``hasDocuments``), *retrieved*
    (``documentReads.pages > 0``), *digestOnly* (``documentDigests > 0``),
    else *none* — in that precedence, since a retrieving call still holds
    the digest. ``n`` per class is ``up + down``; the down-thumb rate is
    ``down / n``."""
    model_config = ConfigDict(populate_by_name=True)

    full: FeedbackCounts = Field(default_factory=FeedbackCounts)
    digest_only: FeedbackCounts = Field(default_factory=FeedbackCounts, alias="digestOnly")
    retrieved: FeedbackCounts = Field(default_factory=FeedbackCounts)
    none: FeedbackCounts = Field(default_factory=FeedbackCounts)


class FeedbackProfile(BaseModel):
    """The outcome signal joined to the session's cost rows. ``byTurnClass``
    is ``None`` when no cost row carries the turn-class fields (they arrive
    with #1137; rows written before it have none) — "not tracked", not zero.
    ``unjoined`` counts thumbs whose message has no cost row at all (the row
    expired, or the call was never recorded)."""
    model_config = ConfigDict(populate_by_name=True)

    up: int = 0
    down: int = 0
    by_turn_class: Optional[FeedbackByTurnClass] = Field(None, alias="byTurnClass")
    unjoined: int = 0
    # Down-thumbs the user followed with a retry-with-correction, and what
    # that rework cost: the thumbed call(s) plus the retry turn's calls
    # (response-feedback spec §7 "rework cost"). ``None`` when no retry has
    # a cost row to price.
    retried: int = 0
    rework_usd: Optional[float] = Field(None, alias="reworkUsd")


class DataCoverage(BaseModel):
    """Which optional signals this session actually has, so the UI can say
    "not tracked" instead of rendering an honest-looking zero."""
    model_config = ConfigDict(populate_by_name=True)

    tool_census: bool = Field(False, alias="toolCensus")
    compaction_count: bool = Field(False, alias="compactionCount")
    fingerprints: bool = False
    cost: bool = False
    prefix_tokens: bool = Field(False, alias="prefixTokens")
    window_trim: bool = Field(False, alias="windowTrim")
    compaction_events: bool = Field(False, alias="compactionEvents")
    # Any F# row, or a session rollup written while diagnostics were on.
    feedback: bool = False
    documents: bool = False


class SessionProfile(BaseModel):
    """The content-free diagnostic profile of one conversation."""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    user_id: Optional[str] = Field(None, alias="userId")
    session: UserSessionSummary

    call_count: int = Field(0, alias="callCount")
    peak_context_tokens: Optional[int] = Field(None, alias="peakContextTokens")
    compaction_threshold: int = Field(..., alias="compactionThreshold")
    write_read_ratio: Optional[float] = Field(None, alias="writeReadRatio")

    attachments: AttachmentProfile
    context_trajectory: List[ContextTrajectoryPoint] = Field(
        default_factory=list, alias="contextTrajectory"
    )
    model_mix: Dict[str, int] = Field(default_factory=dict, alias="modelMix")
    fingerprint_changes: FingerprintChanges = Field(
        default_factory=FingerprintChanges, alias="fingerprintChanges"
    )
    tool_census: Dict[str, ToolCensusEntry] = Field(
        default_factory=dict, alias="toolCensus"
    )
    enabled_tool_ids: List[str] = Field(default_factory=list, alias="enabledToolIds")

    diagnoses: List[SessionDiagnosis] = Field(default_factory=list)
    data_coverage: DataCoverage = Field(default_factory=DataCoverage, alias="dataCoverage")
    # Latest recorded static prefix split (system prompt vs tool schemas).
    prefix_tokens: Optional[PrefixTokens] = Field(None, alias="prefixTokens")
    # How many calls in this session were preceded by a window trim, and the
    # messages the window has removed in total (last ledger-bearing call).
    window_trim_calls: int = Field(0, alias="windowTrimCalls")
    window_removed_messages: Optional[int] = Field(None, alias="windowRemovedMessages")
    # Compaction decisions by kind across the session's calls.
    compaction_event_counts: Dict[str, int] = Field(
        default_factory=dict, alias="compactionEventCounts"
    )
    # The summary's token size at the most recent compaction decision.
    last_summary_tokens: Optional[int] = Field(None, alias="lastSummaryTokens")
    # Thumbs up/down joined to the cost rows by (sessionId, messageId).
    feedback: FeedbackProfile = Field(default_factory=FeedbackProfile)
    # Document lifecycle across the session's calls: how many calls ran with
    # the full document inline vs. a digest only (the digest-vs-full turn
    # shares), the largest estimated document footprint seen, and what
    # document_read pulled back in total.
    full_document_calls: int = Field(0, alias="fullDocumentCalls")
    digest_only_calls: int = Field(0, alias="digestOnlyCalls")
    peak_document_tokens: Optional[int] = Field(None, alias="peakDocumentTokens")
    document_read_calls: int = Field(0, alias="documentReadCalls")
    document_read_pages: int = Field(0, alias="documentReadPages")
