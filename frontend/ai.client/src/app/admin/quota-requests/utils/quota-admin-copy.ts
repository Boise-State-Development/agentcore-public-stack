/**
 * Wording and small rules shared by the admin quota-request surfaces.
 *
 * Dates and amounts go through the user side's `quota-format.ts`, so an admin
 * and the requester read the same "$25 per month through Nov 30, 2026".
 */
import { formatAmount, formatQuotaDate } from '../../../services/quota/quota-format';
import { FundingField, FundingSource, QuotaRequestStatus } from '../models/quota-requests-admin.models';

export const STATUS_LABELS: Record<QuotaRequestStatus, string> = {
  pending: 'Pending',
  info_requested: 'Waiting on user',
  approved: 'Approved',
  denied: 'Denied',
  withdrawn: 'Withdrawn',
};

export const STATUS_PILL_CLASSES: Record<QuotaRequestStatus, string> = {
  pending: 'bg-state-warning-100 text-state-warning-900 dark:bg-state-warning-900/30 dark:text-state-warning-200',
  info_requested: 'bg-state-info-100 text-state-info-900 dark:bg-state-info-900/30 dark:text-state-info-200',
  approved: 'bg-state-success-100 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-200',
  denied: 'bg-gray-100 text-gray-800 dark:bg-white/10 dark:text-gray-200',
  withdrawn: 'bg-gray-100 text-gray-800 dark:bg-white/10 dark:text-gray-200',
};

/**
 * "Fund 100000 · Org 4210", from the schema's labels in schema order, or the
 * free-text label when the deployment collects no fields. Empty for no source.
 */
export function describeFundingSource(source: FundingSource | null | undefined, fields: FundingField[] = []): string {
  if (!source) return '';
  const values = source.fields ?? {};
  const known = fields
    .filter(f => (values[f.key] ?? '').trim())
    .map(f => `${f.label} ${values[f.key].trim()}`);
  const unknown = Object.entries(values)
    .filter(([key, value]) => value?.trim() && !fields.some(f => f.key === key))
    .map(([key, value]) => `${key} ${value.trim()}`);
  const coded = [...known, ...unknown].join(' · ');
  const label = source.label?.trim() ?? '';
  if (coded && label) return `${label} (${coded})`;
  return coded || label;
}

/**
 * Why a funding value is refused, or null. A `pattern` is matched against the
 * whole value, as the backend does.
 */
export function fundingFieldError(field: FundingField, value: string | undefined): string | null {
  const trimmed = (value ?? '').trim();
  if (!trimmed) return field.required ? `${field.label} is required` : null;
  if (!field.pattern) return null;
  try {
    return new RegExp(`^(?:${field.pattern})$`).test(trimmed) ? null : `${field.label} is not in the expected format`;
  } catch {
    // A pattern the browser cannot compile is the server's call, not a reason to block the form.
    return null;
  }
}

/** Only the fields with a value, trimmed; what the API stores. */
export function cleanFundingValues(values: Record<string, string>): Record<string, string> {
  const cleaned: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    const trimmed = (value ?? '').trim();
    if (trimmed) cleaned[key] = trimmed;
  }
  return cleaned;
}

/**
 * The confirmation for a standing chargeback (spec §6.4): one sentence naming
 * the user, the fund and the cap, because this commits someone else's money.
 */
export function standingConfirmation(args: {
  user: string;
  fund: string;
  cap: number | null;
  through: string;
  per: string;
}): string {
  const limit = args.cap === null ? 'with no cap' : `up to ${formatAmount(args.cap)} ${args.per}`;
  return (
    `After their included quota, ${args.user}'s usage will be charged to ${args.fund}, ` +
    `${limit}, through ${formatQuotaDate(args.through)}.`
  );
}

/** The extension's amount as the tables read it: "+$25 per month", "cap $200", "no cap". */
export function describeExtensionAmount(amount: number | null, standing: boolean, per = 'per month'): string {
  if (amount === null || Number.isNaN(amount)) return 'No cap';
  return standing ? `Cap ${formatAmount(amount)} ${per}` : `+${formatAmount(amount)} ${per}`;
}

/** "2 h", "3 d": how long a request has waited. */
export function age(iso: string, now: Date = new Date()): string {
  const then = new Date(iso.replace('+00:00Z', 'Z'));
  if (Number.isNaN(then.getTime())) return '';
  const minutes = Math.max(0, Math.round((now.getTime() - then.getTime()) / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}
