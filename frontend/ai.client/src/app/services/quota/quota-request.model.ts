/**
 * Quota requests (docs/specs/quota-requests-and-extensions.md §7, §10.1).
 *
 * Mirrors `backend/src/apis/shared/quota_requests/models.py`. The `/quota/*`
 * routes serialize every amount as a decimal **string** (`"25"`, `"10.00"`),
 * like the quota admin API; {@link toAmount} turns one into a number at the
 * edge so nothing past this file handles strings. `/costs/quota-status` is
 * the exception and sends plain numbers (see `quota-status.model.ts`).
 */

export type QuotaRequestStatus = 'pending' | 'info_requested' | 'approved' | 'denied' | 'withdrawn';
export type QuotaRequestKind = 'one_time' | 'standing';
export type FundingSourceMode = 'hidden' | 'optional' | 'required';

/** A decimal as the API sends it. */
export type DecimalString = string | number;

/** `"25"` → 25. Anything unparseable is NaN, so a comparison against it is false. */
export function toAmount(value: DecimalString | null | undefined): number {
  if (value === null || value === undefined || value === '') return NaN;
  return typeof value === 'number' ? value : Number(value);
}

/** One field of the deployment's funding-source schema (§4.3). */
export interface FundingField {
  key: string;
  label: string;
  required: boolean;
  pattern?: string | null;
  help?: string | null;
}

export interface FundingSource {
  label?: string | null;
  fields: Record<string, string>;
  authorizerEmail?: string | null;
}

/** `GET /quota/policy`: the subset of the policy that shapes the request dialog. */
export interface QuotaRequestPolicy {
  amountPresets: DecimalString[];
  allowCustomAmount: boolean;
  /** 0 means no cap on what a user may ask for. */
  maxRequestAmount: DecimalString;
  /** `end_of_period` or `days:N`. */
  defaultThrough: string;
  maxDurationDays: number;
  justificationMinChars: number;
  fundingSourceMode: FundingSourceMode;
  fundingFields: FundingField[];
  /** Whether the user may ask for an ongoing (standing) arrangement. */
  standingEnabled: boolean;
  /** `MM-DD`. */
  fiscalYearEnd: string;
}

export interface QuotaRequestDecision {
  decidedAt: string;
  outcome: 'approved' | 'denied';
  note?: string | null;
  amount?: DecimalString | null;
  validUntil?: string | null;
  fundingMode?: string | null;
  standing?: boolean | null;
}

export interface QuotaRequest {
  requestId: string;
  status: QuotaRequestStatus;
  kind: QuotaRequestKind;
  requestedAmount: DecimalString;
  /** ISO date, inclusive. */
  requestedUntil: string;
  justification: string;
  fundingSource?: FundingSource | null;
  /** The note an admin sent with `info_requested`. */
  adminNote?: string | null;
  decision?: QuotaRequestDecision | null;
  createdAt: string;
  updatedAt: string;
}

export interface QuotaRequestCreate {
  kind: QuotaRequestKind;
  requestedAmount: number;
  /** `YYYY-MM-DD`. */
  requestedUntil: string;
  justification: string;
  fundingSource?: FundingSource | null;
}

export const OPEN_REQUEST_STATUSES: ReadonlySet<string> = new Set(['pending', 'info_requested']);
