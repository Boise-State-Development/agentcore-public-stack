/**
 * The authenticated user's own quota status, from GET /costs/quota-status.
 *
 * Mirrors backend UserQuotaStatusResponse. Three states the UI distinguishes:
 *  - configured === false        -> no tier assigned; show nothing / a note
 *  - unlimited === true          -> no denominator; show usage without a %
 *  - normal (monthlyLimit set)   -> real bar + percentage
 *
 * `monthlyLimit` is the *effective* limit (base + extensions); the extension
 * fields split it (docs/specs/quota-requests-and-extensions.md §7.4). They are
 * optional here because a backend older than the extensions work omits them.
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
  /** The tier (or override) limit, before extensions. */
  baseLimit?: number | null;
  /** Base plus every active extension; null under an uncapped standing extension. */
  effectiveLimit?: number | null;
  /** Active extensions, ordered by `validUntil`. */
  extensions?: QuotaStatusExtension[];
  /** Spend this period charged to a fund, when a chargeback extension is being consumed. */
  charged?: QuotaStatusCharged | null;
  /** The caller's open quota request; only while quota requests are switched on. */
  openRequest?: QuotaStatusOpenRequest | null;
}

export interface QuotaStatusExtension {
  /** Per period; null is an uncapped standing extension. */
  amount: number | null;
  /** ISO datetime, inclusive. */
  validUntil: string;
  fundingMode: 'central' | 'chargeback' | string;
  standing: boolean;
  fundingLabel?: string | null;
}

export interface QuotaStatusCharged {
  label: string;
  thisPeriod: number;
  cap?: number | null;
}

export interface QuotaStatusOpenRequest {
  requestId: string;
  status: 'pending' | 'info_requested' | string;
  requestedAmount: number;
  createdAt: string;
  adminNote?: string | null;
}
