/**
 * Cost tracking models matching backend Pydantic models
 */

/**
 * Detailed cost breakdown by token type
 */
export interface CostBreakdown {
  inputCost: number;
  outputCost: number;
  cacheWriteCost: number;
  cacheReadCost: number;
  totalCost: number;
}

/**
 * Cost summary for a specific model
 */
export interface ModelCostSummary {
  modelId: string;
  modelName: string;
  provider: string;

  // Token usage
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCacheReadTokens: number;
  totalCacheWriteTokens: number;

  // Cost
  costBreakdown: CostBreakdown;

  // Stats
  requestCount: number;
}

/**
 * Aggregated cost summary for a user
 */
export interface UserCostSummary {
  userId: string;

  // Time range
  periodStart: string;
  periodEnd: string;

  // Aggregate costs
  totalCost: number;

  // Per-model breakdown
  models: ModelCostSummary[];

  // Overall token usage
  totalRequests: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCacheSavings: number;
}
