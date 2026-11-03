/**
 * AWS Limits API shapes (`/admin/aws-limits`, GitHub issue #12). Mirrors
 * backend/src/apis/shared/aws_limits/models.py, camelCase.
 */

export interface DocLink {
  label: string;
  url: string;
}

export interface AwsLimit {
  serviceCode: string;
  serviceName: string;
  quotaCode: string;
  quotaName: string;
  description: string | null;
  appliedValue: number | null;
  defaultValue: number | null;
  unit: string | null;
  periodValue: number | null;
  periodUnit: string | null;
  adjustable: boolean;
  globalQuota: boolean;
  hasUsageMetric: boolean;
  /** Peak over the last hour, in the quota's own unit and period. Null without a usage metric. */
  usage: number | null;
  /** usage / applied value, percent. */
  utilization: number | null;
  consoleUrl: string;
  /** Adjustable and in the monitored catalog. */
  requestable: boolean;
}

export interface AwsLimitGroup {
  id: string;
  label: string;
  description: string;
  limits: AwsLimit[];
  docLinks: DocLink[];
  error: string | null;
  usageError: string | null;
  fetchedAt: string | null;
}

export interface AutoManagementStatus {
  optInStatus: string | null;
  optInLevel: string | null;
  optInType: string | null;
  consoleUrl: string;
  error: string | null;
}

export interface AwsLimitsSummary {
  monitored: number;
  atOrAbove80: number;
  atOrAbove95: number;
  openRequests: number;
}

export interface AwsLimitsDashboard {
  region: string;
  generatedAt: string;
  asOf: string;
  allBedrock: boolean;
  /** Whether this user may submit increase requests. The server decides; the UI follows. */
  canRequestIncrease: boolean;
  groups: AwsLimitGroup[];
  summary: AwsLimitsSummary;
  autoManagement: AutoManagementStatus | null;
  guidanceUrl: string;
}

export type IncreaseRequestStatus =
  | 'PENDING'
  | 'CASE_OPENED'
  | 'APPROVED'
  | 'DENIED'
  | 'NOT_APPROVED'
  | 'CASE_CLOSED'
  | 'INVALID_REQUEST';

export interface IncreaseRequestBody {
  serviceCode: string;
  quotaCode: string;
  desiredValue: number;
}

export interface IncreaseRequestRecord {
  requestId: string;
  serviceCode: string;
  serviceName: string | null;
  quotaCode: string;
  quotaName: string | null;
  desiredValue: number | null;
  status: IncreaseRequestStatus | string;
  requestType: string | null;
  caseId: string | null;
  created: string | null;
  lastUpdated: string | null;
  unit: string | null;
  /** The admin who asked through this app; null for console, CLI or Automatic Management requests. */
  requestedBy: string | null;
  consoleUrl: string;
}

export interface IncreaseHistory {
  region: string;
  requests: IncreaseRequestRecord[];
  asOf: string;
}

export const WARN_THRESHOLD = 80;
export const DANGER_THRESHOLD = 95;
