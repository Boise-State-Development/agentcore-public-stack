import { AwsLimit, IncreaseRequestStatus } from '../models';

const NUMBER = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });

export function formatLimitValue(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return NUMBER.format(value);
}

/** "Count", "per second", "GB per minute"… Service Quotas reports "None" for unitless quotas. */
export function unitAndPeriod(limit: Pick<AwsLimit, 'unit' | 'periodUnit' | 'periodValue'>): string {
  const unit = limit.unit && limit.unit !== 'None' ? limit.unit : '';
  const period = limit.periodUnit
    ? `per ${limit.periodValue && limit.periodValue > 1 ? `${limit.periodValue} ` : ''}${limit.periodUnit.toLowerCase()}${limit.periodValue && limit.periodValue > 1 ? 's' : ''}`
    : '';
  return [unit, period].filter(Boolean).join(' ') || 'Count';
}

export function raisedFromDefault(limit: Pick<AwsLimit, 'appliedValue' | 'defaultValue'>): boolean {
  return (
    limit.defaultValue !== null &&
    limit.appliedValue !== null &&
    limit.defaultValue !== limit.appliedValue
  );
}

export const STATUS_LABELS: Record<IncreaseRequestStatus, string> = {
  PENDING: 'Pending',
  CASE_OPENED: 'Case opened',
  APPROVED: 'Approved',
  DENIED: 'Denied',
  NOT_APPROVED: 'Not approved',
  CASE_CLOSED: 'Case closed',
  INVALID_REQUEST: 'Invalid request',
};

const INFO = 'bg-state-info-100 text-state-info-900 dark:bg-state-info-900/30 dark:text-state-info-200';
const WARNING = 'bg-state-warning-100 text-state-warning-900 dark:bg-state-warning-900/30 dark:text-state-warning-200';
const SUCCESS = 'bg-state-success-100 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-200';
const DANGER = 'bg-state-danger-100 text-state-danger-900 dark:bg-state-danger-900/30 dark:text-state-danger-200';
const NEUTRAL = 'bg-gray-100 text-gray-800 dark:bg-white/10 dark:text-gray-200';

export const STATUS_PILL_CLASSES: Record<IncreaseRequestStatus, string> = {
  PENDING: INFO,
  CASE_OPENED: WARNING,
  APPROVED: SUCCESS,
  DENIED: DANGER,
  NOT_APPROVED: DANGER,
  INVALID_REQUEST: DANGER,
  CASE_CLOSED: NEUTRAL,
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as IncreaseRequestStatus] ?? status;
}

export function statusClass(status: string): string {
  return STATUS_PILL_CLASSES[status as IncreaseRequestStatus] ?? NEUTRAL;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' });
}

/** "just now", "4 min ago", "2 h ago". */
export function ageLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}
