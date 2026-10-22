/**
 * The authenticated user's own quota status, from GET /costs/quota-status.
 *
 * Mirrors backend UserQuotaStatusResponse. Three states the UI distinguishes:
 *  - configured === false        -> no tier assigned; show nothing / a note
 *  - unlimited === true          -> no denominator; show usage without a %
 *  - normal (monthlyLimit set)   -> real bar + percentage
 */
export interface QuotaStatus {
  configured: boolean;
  unlimited: boolean;
  tierName: string | null;
  matchedBy: string | null;
  monthlyLimit: number | null;
  currentUsage: number;
  remaining: number | null;
  usagePercentage: number;
  periodType: string;
  resetInfo: string | null;
  hasActiveOverride: boolean;
}
