"""Response models for the release-ledger admin API (docs/specs/release-ledger.md §6.1, §6.3).

Content-free by the D7 contract: ids (version, build, model, tool, agent,
project), dates and numbers. No user id, session id, message id, title,
prompt, answer, tool argument or tool result. ``tests/apis/app_api/admin/costs/test_content_policy.py``
walks every model here, and a populated readout, against that rule.

Every KPI is ``{value, n}``: ``value`` is ``None`` below its floor, never ``0``
(``apis.shared.costs.ledger_kpis``). Every readout pair is ``[baseline, candidate]``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class LedgerKpi(_Model):
    value: Optional[float] = None
    n: int = 0


class LedgerDeployMarker(_Model):
    service: str
    first_seen_at: str = Field("", alias="firstSeenAt")


class LedgerBuild(_Model):
    """One ``(appVersion, build)`` in the range."""

    app_version: str = Field(..., alias="appVersion")
    build: str
    build_kind: str = Field(..., alias="buildKind", description="image (a stamped content hash), commit (a backfill label from the deploy map) or unknown")
    first_date: str = Field(..., alias="firstDate")
    last_date: str = Field(..., alias="lastDate")
    days_observed: int = Field(..., alias="daysObserved")
    first_live_at: Optional[str] = Field(None, alias="firstLiveAt")
    backfilled: bool = False
    deploy_markers: List[LedgerDeployMarker] = Field(default_factory=list, alias="deployMarkers")
    totals: Dict[str, float] = Field(default_factory=dict)
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)


class LedgerVersion(_Model):
    """One ``appVersion`` in the range, over all of its builds."""

    app_version: str = Field(..., alias="appVersion")
    first_date: str = Field(..., alias="firstDate")
    last_date: str = Field(..., alias="lastDate")
    days_observed: int = Field(..., alias="daysObserved")
    builds: int = 0
    first_live_at: Optional[str] = Field(None, alias="firstLiveAt")
    totals: Dict[str, float] = Field(default_factory=dict)
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)


class LedgerBackfillMarker(_Model):
    from_at: str = Field("", alias="from")
    to_at: str = Field("", alias="to")
    rows: int = 0
    ledger_rows: int = Field(0, alias="ledgerRows")
    status: str = ""
    started_at: Optional[str] = Field(None, alias="startedAt")
    completed_at: Optional[str] = Field(None, alias="completedAt")


class LedgerVersionsResponse(_Model):
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    versions: List[LedgerVersion] = Field(default_factory=list)
    builds: List[LedgerBuild] = Field(default_factory=list)
    backfills: List[LedgerBackfillMarker] = Field(default_factory=list)


class LedgerSeriesPoint(_Model):
    date: str
    totals: Dict[str, float] = Field(default_factory=dict)
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)


class LedgerSeries(_Model):
    key: str
    points: List[LedgerSeriesPoint] = Field(default_factory=list)


class LedgerVersionBand(_Model):
    key: str
    app_version: str = Field(..., alias="appVersion")
    build: Optional[str] = None
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")


class LedgerSeriesResponse(_Model):
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    by: str
    unit: str
    series: List[LedgerSeries] = Field(default_factory=list)
    bands: List[LedgerVersionBand] = Field(default_factory=list)
    omitted_keys: int = Field(0, alias="omittedKeys")


class LedgerTool(_Model):
    tool_id: str = Field(..., alias="toolId")
    totals: Dict[str, float] = Field(default_factory=dict)
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)
    payload_measured: bool = Field(..., alias="payloadMeasured")
    spec_tokens: Optional[int] = Field(None, alias="specTokens")


class LedgerToolsResponse(_Model):
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    version: Optional[str] = None
    tools: List[LedgerTool] = Field(default_factory=list)
    not_measured: List[str] = Field(default_factory=list, alias="notMeasured")


class LedgerGroup(_Model):
    id: str
    totals: Dict[str, float] = Field(default_factory=dict)
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)


class LedgerGroupsResponse(_Model):
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    version: Optional[str] = None
    kind: str
    rows: List[LedgerGroup] = Field(default_factory=list)
    omitted: int = 0


class LedgerThumbs(_Model):
    """Counts always; a rate only at ``n >= minimumN`` (spec §10 Q2)."""

    up: int = 0
    down: int = 0
    n: int = 0
    down_rate: Optional[float] = Field(None, alias="downRate")
    below_floor: bool = Field(True, alias="belowFloor")
    retries: int = 0


class LedgerProvenance(_Model):
    verdicts: int = 0
    claims: int = 0
    supported: int = 0
    quote_unsupported: int = Field(0, alias="quoteUnsupported")
    unassessed: int = 0
    unverified: int = 0
    quote_unsupported_share: LedgerKpi = Field(default_factory=LedgerKpi, alias="quoteUnsupportedShare")


class LedgerQualityUnit(_Model):
    key: str
    app_version: str = Field(..., alias="appVersion")
    build: Optional[str] = None
    first_date: str = Field(..., alias="firstDate")
    last_date: str = Field(..., alias="lastDate")
    kpis: Dict[str, LedgerKpi] = Field(default_factory=dict)
    outcomes: Dict[str, int] = Field(default_factory=dict)
    eval_runs: List[Dict[str, Any]] = Field(default_factory=list, alias="eval")
    smoke_runs: List[Dict[str, Any]] = Field(default_factory=list, alias="smoke")
    thumbs: LedgerThumbs = Field(default_factory=LedgerThumbs)
    provenance: LedgerProvenance = Field(default_factory=LedgerProvenance)


class LedgerQualityResponse(_Model):
    from_date: str = Field(..., alias="from")
    to_date: str = Field(..., alias="to")
    unit: str
    minimum_n: int = Field(20, alias="minimumN")
    units: List[LedgerQualityUnit] = Field(default_factory=list)
    thumbs_coverage: Dict[str, Any] = Field(default_factory=dict, alias="thumbsCoverage")
    provenance_coverage: Dict[str, Any] = Field(default_factory=dict, alias="provenanceCoverage")


# ---------------------------------------------------------------------------
# Readout (§6.3)
# ---------------------------------------------------------------------------


class ReadoutSide(_Model):
    key: str
    app_version: str = Field(..., alias="appVersion")
    build: Optional[str] = None
    builds: int = 0
    window: List[Optional[str]]
    days: int
    full_days: int = Field(0, alias="fullDays")


class ReadoutKpi(_Model):
    baseline: Optional[float] = None
    candidate: Optional[float] = None
    delta: Optional[float] = None
    n: List[int] = Field(default_factory=lambda: [0, 0])
    by_model: Optional[Dict[str, "ReadoutKpi"]] = Field(None, alias="byModel")


class ReadoutMixShift(_Model):
    warn: bool = False
    models: Dict[str, List[float]] = Field(default_factory=dict)
    active_users: List[Optional[float]] = Field(default_factory=lambda: [None, None], alias="activeUsers")


class ReadoutTool(_Model):
    tool_id: str = Field(..., alias="toolId")
    calls: List[int]
    error_rate: List[Optional[float]] = Field(..., alias="errorRate")
    result_chars_per_call: List[Optional[float]] = Field(..., alias="resultCharsPerCall")
    mean_duration_ms: List[Optional[float]] = Field(..., alias="meanDurationMs")
    est_input_tokens: List[Optional[float]] = Field(..., alias="estInputTokens")
    est_input_usd: List[Optional[float]] = Field(..., alias="estInputUsd")
    payload_measured: List[Optional[bool]] = Field(..., alias="payloadMeasured")
    spec_tokens: Optional[int] = Field(None, alias="specTokens")


class ReadoutGroup(_Model):
    id: str
    calls: List[int]
    turns: List[int]
    cost: List[float]
    cost_per_turn: ReadoutKpi = Field(..., alias="costPerTurn")
    waste_per_turn: ReadoutKpi = Field(..., alias="wastePerTurn")
    tool_error_rate: List[Optional[float]] = Field(..., alias="toolErrorRate")


class ReadoutLatencyMetric(_Model):
    p50: List[Optional[float]]
    p90: List[Optional[float]]
    n: List[int]


class ReadoutLatency(_Model):
    available: bool = False
    server_first_token_ms: Optional[ReadoutLatencyMetric] = Field(None, alias="serverFirstTokenMs")
    client_first_token_ms: Optional[ReadoutLatencyMetric] = Field(None, alias="clientFirstTokenMs")
    probe_cold_first_token_ms: Optional[ReadoutLatencyMetric] = Field(None, alias="probeColdFirstTokenMs")
    probe_follow_up_first_token_ms: Optional[ReadoutLatencyMetric] = Field(None, alias="probeFollowUpFirstTokenMs")


class ReadoutPairs(_Model):
    """Paired eval/smoke rows, ``LEDGER#EVAL`` / ``#SMOKE`` items as published (PR-5)."""

    baseline: List[Dict[str, Any]] = Field(default_factory=list)
    candidate: List[Dict[str, Any]] = Field(default_factory=list)


class ReadoutThumbs(_Model):
    up: List[int]
    down: List[int]
    n: List[int]
    down_rate: List[Optional[float]] = Field(..., alias="downRate")
    retries: List[int]
    minimum_n: int = Field(20, alias="minimumN")
    coverage: Optional[Dict[str, Any]] = None


class ReadoutProvenance(_Model):
    claims: List[int]
    quote_unsupported: List[int] = Field(..., alias="quoteUnsupported")
    quote_unsupported_share: List[Optional[float]] = Field(..., alias="quoteUnsupportedShare")
    coverage: Optional[Dict[str, Any]] = None


class ReadoutQuality(_Model):
    eval_runs: ReadoutPairs = Field(default_factory=ReadoutPairs, alias="eval")
    smoke_runs: ReadoutPairs = Field(default_factory=ReadoutPairs, alias="smoke")
    empty_answer_share: List[Optional[float]] = Field(..., alias="emptyAnswerShare")
    interrupted_share: List[Optional[float]] = Field(..., alias="interruptedShare")
    paused_share: List[Optional[float]] = Field(..., alias="pausedShare")
    forced_compaction_rate: List[Optional[float]] = Field(..., alias="forcedCompactionRate")
    tool_error_rate: List[Optional[float]] = Field(..., alias="toolErrorRate")
    outcomes: Dict[str, List[int]] = Field(default_factory=dict)
    provenance: ReadoutProvenance
    thumbs: ReadoutThumbs


class ReadoutPlatform(_Model):
    month: str
    available: bool = False
    inference_cost: float = Field(0.0, alias="inferenceCost")
    platform_cost: float = Field(0.0, alias="platformCost")
    total_cost: float = Field(0.0, alias="totalCost")


class ReadoutCaveat(_Model):
    code: str
    message: str


class LedgerReadout(_Model):
    candidate: ReadoutSide
    baseline: Optional[ReadoutSide] = None
    unit: str
    requested_days: int = Field(..., alias="requestedDays")
    mix_shift: ReadoutMixShift = Field(..., alias="mixShift")
    kpis: Dict[str, ReadoutKpi] = Field(default_factory=dict)
    tools: List[ReadoutTool] = Field(default_factory=list)
    agents: List[ReadoutGroup] = Field(default_factory=list)
    projects: List[ReadoutGroup] = Field(default_factory=list)
    latency: ReadoutLatency = Field(default_factory=ReadoutLatency)
    quality: ReadoutQuality
    platform: Optional[ReadoutPlatform] = None
    caveats: List[ReadoutCaveat] = Field(default_factory=list)


ReadoutKpi.model_rebuild()
