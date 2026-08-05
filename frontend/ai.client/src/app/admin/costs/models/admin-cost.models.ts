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

export interface TrendsRequestOptions {
  startDate: string;
  endDate: string;
}

export interface ExportRequestOptions {
  period?: string;
  format: 'csv' | 'json';
}
