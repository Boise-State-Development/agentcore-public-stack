/**
 * Wording shared by the quota request surfaces (quota-requests spec §5.5).
 *
 * Quota periods and request dates are UTC on the backend (a month is the UTC
 * month, `requestedUntil` an inclusive UTC date), so dates here are read and
 * written in UTC: a user west of Greenwich must not see "through Nov 29" for
 * a request through Nov 30.
 */

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** 25 → "$25.00". */
export function formatUsd(value: number): string {
  return USD.format(value);
}

/** 25 → "$25", 12.5 → "$12.50": whole amounts read as people say them. */
export function formatAmount(value: number): string {
  return Number.isInteger(value) ? `$${value}` : USD.format(value);
}

/** "per month" or "per day", from the tier's period type. Dollar amounts always carry one. */
export function perPeriod(periodType: string | null | undefined): string {
  return periodType === 'daily' ? 'per day' : 'per month';
}

/** "monthly" or "daily". */
export function periodAdjective(periodType: string | null | undefined): string {
  return periodType === 'daily' ? 'daily' : 'monthly';
}

/** `"2026-11-30"` or an ISO datetime → "Nov 30, 2026" (UTC). Empty for an unparseable value. */
export function formatQuotaDate(value: string | null | undefined, withYear = true): string {
  if (!value) return '';
  const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value.replace('+00:00Z', 'Z'));
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(withYear ? { year: 'numeric' } : {}),
    timeZone: 'UTC',
  });
}

/** A `Date` → `"YYYY-MM-DD"` in UTC. */
export function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** The last day of the period that contains `today`: the UTC month's end, or today for a daily tier. */
export function endOfPeriod(today: Date, periodType: string | null | undefined): string {
  if (periodType === 'daily') return isoDate(today);
  return isoDate(new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() + 1, 0)));
}

/** `today` plus `days`, as `"YYYY-MM-DD"`. */
export function addDays(today: Date, days: number): string {
  return isoDate(new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() + days)));
}

/** The next occurrence of `MM-DD` on or after `today`. */
export function nextOccurrence(today: Date, monthDay: string): string {
  const [month, day] = monthDay.split('-').map(Number);
  const year = today.getUTCFullYear();
  const candidate = isoDate(new Date(Date.UTC(year, month - 1, day)));
  return candidate >= isoDate(today) ? candidate : isoDate(new Date(Date.UTC(year + 1, month - 1, day)));
}

/** Whether `date` (`YYYY-MM-DD`) ends partway through a monthly period. */
export function endsMidMonth(date: string): boolean {
  const parsed = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return false;
  const next = new Date(parsed.getTime() + 86_400_000);
  return next.getUTCMonth() === parsed.getUTCMonth();
}
