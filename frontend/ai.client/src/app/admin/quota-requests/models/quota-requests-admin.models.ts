/**
 * Admin side of quota requests (docs/specs/quota-requests-and-extensions.md §7, §10.2).
 *
 * Mirrors `backend/src/apis/shared/quota_requests/models.py` and
 * `QuotaExtension` in `agents/main_agent/quota/models.py`. Like the user side,
 * every amount arrives as a decimal string; the shared {@link toAmount} turns
 * one into a number where it is read, so nothing here re-implements parsing.
 */
import type {
  DecimalString,
  FundingField,
  FundingSource,
  FundingSourceMode,
  QuotaRequestKind,
  QuotaRequestStatus,
} from '../../../services/quota/quota-request.model';

export type { DecimalString, FundingField, FundingSource, FundingSourceMode, QuotaRequestKind, QuotaRequestStatus };
export { toAmount } from '../../../services/quota/quota-request.model';

export type FundingMode = 'central' | 'chargeback';

/** What the user saw when they asked (§7.1 `context`). Numbers only. */
export interface QuotaRequestContext {
  tierId?: string | null;
  tierName?: string | null;
  periodType: string;
  period: string;
  baseLimit?: DecimalString | null;
  effectiveLimit?: DecimalString | null;
  currentUsage: DecimalString;
  percentageUsed: DecimalString;
  blocked: boolean;
}

export interface AdminQuotaRequestDecision {
  decidedBy: string;
  decidedByEmail?: string | null;
  decidedAt: string;
  outcome: 'approved' | 'denied';
  note?: string | null;
  amount?: DecimalString | null;
  validUntil?: string | null;
  fundingMode?: FundingMode | null;
  standing?: boolean | null;
  extensionId?: string | null;
}

/** A request as the queue sees it: the user's own view plus who asked and their context. */
export interface AdminQuotaRequest {
  requestId: string;
  userId: string;
  userEmail: string;
  status: QuotaRequestStatus;
  kind: QuotaRequestKind;
  requestedAmount: DecimalString;
  /** ISO date, inclusive. */
  requestedUntil: string;
  justification: string;
  fundingSource?: FundingSource | null;
  fundingKey?: string | null;
  context: QuotaRequestContext;
  adminNote?: string | null;
  decision?: AdminQuotaRequestDecision | null;
  createdAt: string;
  updatedAt: string;
}

export interface QuotaRequestListResponse {
  requests: AdminQuotaRequest[];
  nextCursor: string | null;
}

export interface QuotaRequestSummary {
  pending: number;
  infoRequested: number;
}

export interface QuotaExtension {
  extensionId: string;
  userId: string;
  userEmail: string;
  standing: boolean;
  /** Per period; null is an uncapped standing extension. */
  amount: DecimalString | null;
  validFrom: string;
  validUntil: string;
  fundingMode: FundingMode;
  fundingSource?: FundingSource | null;
  fundingKey?: string | null;
  requestId?: string | null;
  reason: string;
  baseLimitSnapshot?: DecimalString | null;
  createdBy: string;
  createdByEmail?: string | null;
  createdAt: string;
  enabled: boolean;
  revokedAt?: string | null;
  revokedBy?: string | null;
  revokeReason?: string | null;
}

/** `GET /admin/quota-requests/{id}`: the request plus live context (§6.3). */
export interface QuotaRequestDetail {
  request: AdminQuotaRequest;
  user: {
    userId: string;
    email: string;
    name: string;
    roles: string[];
    matchedBy: string | null;
  };
  /** The same shape as `request.context`, read now. */
  live: QuotaRequestContext;
  previousPeriods: { period: string; spend: number }[];
  priorRequests: AdminQuotaRequest[];
  extensions: QuotaExtension[];
}

/** The whole policy (§4.5), as `GET`/`PUT /admin/quota-requests/policy` carry it. */
export interface AdminQuotaRequestPolicy {
  amountPresets: DecimalString[];
  allowCustomAmount: boolean;
  maxRequestAmount: DecimalString;
  defaultThrough: string;
  maxDurationDays: number;
  justificationMinChars: number;
  fundingSourceMode: FundingSourceMode;
  fundingFields: FundingField[];
  defaultFundingMode: FundingMode;
  standingEnabled: boolean;
  standingDefaultCap: DecimalString;
  /** `MM-DD`. */
  fiscalYearEnd: string;
  reportGroupField?: string | null;
  notifyEmails: string[];
  updatedBy?: string | null;
  updatedAt?: string | null;
}

export interface ApproveRequestBody {
  /** null only for an uncapped standing approval. */
  amount: number | null;
  /** `YYYY-MM-DD`. */
  validUntil: string;
  fundingMode: FundingMode;
  fundingSource?: FundingSource | null;
  standing: boolean;
  note?: string | null;
}

export interface ExtensionCreateBody {
  userId: string;
  standing: boolean;
  amount: number | null;
  validUntil: string;
  fundingMode: FundingMode;
  fundingSource?: FundingSource | null;
  reason: string;
  note?: string | null;
}

export interface ApproveResponse {
  request: AdminQuotaRequest;
  extension: QuotaExtension;
}
