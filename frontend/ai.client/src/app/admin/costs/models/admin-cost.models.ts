/**
 * TypeScript models for admin cost dashboard.
 * Mirrors backend Pydantic models from apis/app_api/admin/costs/models.py
 */

// ========== Model Breakdown ==========

export interface ModelBreakdownItem {
  cost: number;
  requests: number;
}

// ========== Top User Cost ==========

export interface TopUserCost {
  userId: string;
  totalCost: number;
  totalRequests: number;
  lastUpdated: string;

  // Optional enrichment fields
  email?: string;
  tierName?: string;
  quotaLimit?: number;
  quotaPercentage?: number;
}

// ========== System Cost Summary ==========

export interface SystemCostSummary {
  period: string; // "2025-01" or "2025-01-15"
  periodType: 'daily' | 'monthly';

  totalCost: number;
  totalRequests: number;
  activeUsers: number;

  totalInputTokens: number;
  totalOutputTokens: number;
  totalCacheSavings: number;

  modelBreakdown?: Record<string, ModelBreakdownItem>;
  lastUpdated: string;
}

// ========== Model Usage Summary ==========

export interface ModelUsageSummary {
  modelId: string;
  modelName: string;
  provider: string;

  totalCost: number;
  totalRequests: number;
  uniqueUsers: number;
  avgCostPerRequest: number;

  totalInputTokens: number;
  totalOutputTokens: number;
}

// ========== Tier Usage Summary ==========

export interface TierUsageSummary {
  tierId: string;
  tierName: string;

  totalCost: number;
  totalUsers: number;
  usersAtLimit: number;
  usersWarned: number;
  avgUtilization: number;
}

// ========== Cost Trend ==========

export interface CostTrend {
  date: string;
  totalCost: number;
  totalRequests: number;
  activeUsers: number;
}

// ========== Admin Cost Dashboard ==========

export interface AdminCostDashboard {
  currentPeriod: SystemCostSummary;
  topUsers: TopUserCost[];
  modelUsage: ModelUsageSummary[];
  tierUsage?: TierUsageSummary[];
  dailyTrends?: CostTrend[];
}

// ========== Session Cost Anatomy ==========

/**
 * Derived prompt-cache status for one model call.
 * Null on rows persisted before the cache-observability feature.
 */
export type CacheStatus =
  | 'first_write'
  | 'hit'
  /**
   * Read a leading prefix segment, re-wrote the rest against a live cache
   * entry. Costs like a miss despite the nonzero read — this is the shape
   * that used to be reported as `hit` with zero waste.
   */
  | 'partial_miss'
  | 'miss_ttl_expired'
  | 'miss_avoidable'
  | 'uncached';

/** Prompt-cache prefix hashes for one model call. */
export interface PrefixFingerprints {
  toolConfigHash?: string | null;
  systemPromptHash?: string | null;
  historyHash?: string | null;
  messageCount?: number | null;
}

/** One model call within a session's cost anatomy. */
export interface SessionCallRow {
  timestamp: string;
  messageId?: number | null;
  modelId?: string | null;

  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;

  cost: number;
  cacheStatus?: CacheStatus | null;
  cacheGapSeconds?: number | null;
  /**
   * Seconds since the last call with the SAME prefix, present only when that
   * was an older call than the immediately previous one. Absent means the two
   * coincide; present explains a status that would otherwise look inconsistent
   * with `cacheGapSeconds` — e.g. a `miss_ttl_expired` sitting next to a short
   * gap, because an `@`-mention ran in between under a different prefix.
   */
  cachePrefixGapSeconds?: number | null;
  wastedUsd: number;
  /**
   * Which Agent ran this call, and whether that changed from the call before it (#756).
   *
   * An `@`-mention hands one turn to a different Agent, which genuinely re-writes the
   * prefix. On the row that is indistinguishable from the nondeterministic-ordering
   * regression the fingerprints exist to catch — both flip `toolConfigHash` and
   * `systemPromptHash` together — so this is what tells them apart.
   */
  turnAgentId?: string | null;
  agentSwitched?: boolean;
  prefixFingerprints?: PrefixFingerprints | null;
}

/** Per-call cost anatomy for one session (admin cache-miss forensics). */
export interface SessionCostAnatomy {
  sessionId: string;
  calls: SessionCallRow[];

  totalCost: number;
  totalCacheReadTokens: number;
  totalCacheWriteTokens: number;
  avoidableMissCount: number;
  /**
   * Calls that hit a leading prefix segment and re-wrote the rest.
   * `partialMissUsd` is a subset of `wastedUsd`, never deducted from it.
   */
  partialMissCount: number;
  partialMissUsd: number;
  wastedUsd: number;
  /**
   * The subset of the two figures above that an Agent switch explains (#756).
   *
   * A *split*, never a deduction — the totals still carry every dollar spent, because
   * hiding what `@`-mentions cost would understate a feature worth measuring. Subtract
   * for unexplained waste, which is the number a prefix-stability regression moves.
   */
  agentSwitchMissCount: number;
  agentSwitchUsd: number;
  /** cacheRead / (cacheRead + cacheWrite); null until any cache activity. */
  cacheEfficiency: number | null;
}

/**
 * One conversation in the "most expensive sessions" list.
 *
 * ⚠️ `totalCost` is the session's **lifetime** cost, not its cost within the
 * requested period — a runaway thread usually spans period boundaries, and
 * that whole-conversation number is the one support acts on. The period
 * selects which sessions are listed, not how their dollars are summed.
 */
export interface TopSessionCost {
  sessionId: string;
  userId: string;
  title?: string | null;
  totalCost: number;
  lastMessageAt?: string | null;
  createdAt?: string | null;
  messageCount?: number | null;
  lastContextTokens?: number | null;
  /** Prompt-cache waste booked to this session — a platform problem, not a heavy user. */
  partialMissCount?: number | null;
  partialMissUsd?: number | null;
  userPeriodCost?: number | null;
  shareOfUserPeriod?: number | null;
}

/** Most expensive conversations for a period, cost-sorted. */
export interface TopSessionsResponse {
  period: string;
  sessions: TopSessionCost[];
  /** How many top-cost users were fanned out over to build this list. */
  usersScanned: number;
  /** True when more users had period cost than were scanned. */
  truncated: boolean;
}

// ========== Content-free drill-down: user → conversations → profile ==========
//
// Nothing below carries user text or model-generated prose. The backend enforces
// that at the storage boundary (`apis.shared.observability.content_policy`) and
// walks its response models in a test; these interfaces mirror those models.

export type DiagnosisSeverity = 'high' | 'warn' | 'info';

/** One named finding from the backend's diagnosis rules. */
export interface SessionDiagnosis {
  code: string;
  severity: DiagnosisSeverity;
  headline: string;
  /** The numbers the rule compared and the threshold it compared them to. */
  evidence: Record<string, unknown>;
  suggestion: string;
  /** Repo-relative path of the spec or one-pager that argued the rule. */
  ref: string;
}

/**
 * One conversation in a user's content-free conversation list.
 *
 * `costKnown=false` means the cost aggregate was never written — the cost is
 * *unrecorded*, not zero. Optional counters are `null` on rows written before
 * they shipped, so the UI says "not tracked" rather than "0".
 */
export interface UserSessionSummary {
  sessionId: string;
  createdAt?: string | null;
  lastMessageAt?: string | null;
  status?: string | null;
  messageCount?: number | null;
  modelId?: string | null;
  enabledToolCount?: number | null;
  agentBound: boolean;
  lastContextTokens?: number | null;
  contextWindow?: number | null;
  /** lastContextTokens / contextWindow, 0..1, when both are known. */
  contextShare?: number | null;
  totalCost?: number | null;
  costKnown: boolean;
  shareOfUserPeriod?: number | null;
  cacheEfficiency?: number | null;
  wastedUsd?: number | null;
  partialMissUsd?: number | null;
  summarizedTurns?: number | null;
  summaryApproxTokens?: number | null;
  toolCallCount?: number | null;
  toolErrorCount?: number | null;
  compactionCount?: number | null;
  diagnosisCount: number;
  topDiagnosisSeverity?: DiagnosisSeverity | null;
}

export interface UserSessionsResponse {
  userId: string;
  /** The period the list was scoped to (YYYY-MM), or null for all time. */
  period?: string | null;
  /** The user's recorded cost for `period` — the denominator of each row's share. */
  userPeriodCost?: number | null;
  sessions: UserSessionSummary[];
  /** Rows before `limit` was applied. */
  total: number;
  /** Rows whose cost is unrecorded (listed, flagged, trailing under cost-sort). */
  unknownCostCount: number;
}

export type UserSessionsSort = 'cost' | 'recent' | 'context' | 'messages';

export interface UserSessionsRequestOptions {
  period?: string;
  allTime?: boolean;
  sort?: UserSessionsSort;
  limit?: number;
}

/** Per-session upload stats. Never filenames. */
export interface AttachmentProfile {
  count: number;
  totalBytes: number;
  byMime: Record<string, number>;
}

/** One model call's context occupancy (input + cacheRead + cacheWrite). */
export interface ContextTrajectoryPoint {
  callIndex: number;
  timestamp: string;
  contextTokens: number;
  cacheStatus?: CacheStatus | null;
  modelId?: string | null;
  cost?: number | null;
  /** Per-call tool census when recorded: tool name → calls. */
  toolCalls?: Record<string, number> | null;
}

export interface FingerprintChanges {
  systemPrompt: number;
  toolConfig: number;
  /** The subset of the two figures above that an Agent switch explains. */
  explainedByAgentSwitch: number;
}

export interface ToolCensusEntry {
  calls: number;
  errors: number;
}

/** Which optional signals this session actually has ("not tracked" vs "0"). */
export interface DataCoverage {
  toolCensus: boolean;
  compactionCount: boolean;
  fingerprints: boolean;
  cost: boolean;
}

/** The content-free diagnostic profile of one conversation. */
export interface SessionProfile {
  sessionId: string;
  userId?: string | null;
  session: UserSessionSummary;
  callCount: number;
  peakContextTokens?: number | null;
  compactionThreshold: number;
  writeReadRatio?: number | null;
  attachments: AttachmentProfile;
  contextTrajectory: ContextTrajectoryPoint[];
  modelMix: Record<string, number>;
  fingerprintChanges: FingerprintChanges;
  toolCensus: Record<string, ToolCensusEntry>;
  enabledToolIds: string[];
  diagnoses: SessionDiagnosis[];
  dataCoverage: DataCoverage;
}

// ========== API Request Options ==========

export interface DashboardRequestOptions {
  period?: string;
  topUsersLimit?: number;
  includeTrends?: boolean;
}

export interface TopUsersRequestOptions {
  period?: string;
  limit?: number;
  minCost?: number;
  tierId?: string;
}

export interface TopSessionsRequestOptions {
  period?: string;
  limit?: number;
  usersToScan?: number;
  minCost?: number;
}

export interface TrendsRequestOptions {
  startDate: string;
  endDate: string;
}

export interface ExportRequestOptions {
  period?: string;
  format: 'csv' | 'json';
}
