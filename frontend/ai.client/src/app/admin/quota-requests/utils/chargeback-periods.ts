/**
 * Report periods for the chargeback view (quota-requests spec §6.6).
 *
 * A period is a UTC calendar month, `YYYY-MM`, as on the backend. A range is
 * inclusive at both ends and never runs past the current month, which the
 * report refuses because it has not started.
 */

export type ChargebackRangeKind = 'month' | 'quarter' | 'fiscal_year';

/** Hours after a month ends before it closes, so the last cost rows have landed (§9.4). */
export const CLOSE_GRACE_HOURS = 48;

/** `Date` → its UTC month, `YYYY-MM`. */
export function monthOf(date: Date): string {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
}

/** The month before the current one: the report's default (§6.6). */
export function lastCompleteMonth(now: Date): string {
  return monthOf(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1)));
}

function addMonths(period: string, months: number): string {
  const [year, month] = period.split('-').map(Number);
  return monthOf(new Date(Date.UTC(year, month - 1 + months, 1)));
}

/**
 * The months a range covers around `anchor`.
 *
 * A quarter is the calendar quarter holding the anchor; a fiscal year is the
 * twelve months ending in `fiscalYearEnd`'s month (`06-30` → July to June)
 * that hold the anchor. Either is cut at the current month.
 */
export function rangeFor(
  kind: ChargebackRangeKind,
  anchor: string,
  fiscalYearEnd: string,
  now: Date,
): { from: string; to: string } {
  let from = anchor;
  let to = anchor;
  const [year, month] = anchor.split('-').map(Number);
  if (kind === 'quarter') {
    from = monthOf(new Date(Date.UTC(year, Math.floor((month - 1) / 3) * 3, 1)));
    to = addMonths(from, 2);
  } else if (kind === 'fiscal_year') {
    const endMonth = Number(fiscalYearEnd.slice(0, 2)) || 12;
    const endYear = month > endMonth ? year + 1 : year;
    to = `${endYear}-${String(endMonth).padStart(2, '0')}`;
    from = addMonths(to, -11);
  }
  const current = monthOf(now);
  if (to > current) to = current;
  if (from > to) from = to;
  return { from, to };
}

/** `2026-10` → "October 2026". */
export function formatPeriod(period: string, style: 'long' | 'short' = 'long'): string {
  const [year, month] = period.split('-').map(Number);
  if (!year || !month) return period;
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString('en-US', {
    month: style,
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/** When a month closes: 48 hours after its last instant. */
export function closesAt(period: string): Date {
  const [year, month] = period.split('-').map(Number);
  return new Date(Date.UTC(year, month, 1) + CLOSE_GRACE_HOURS * 3_600_000);
}
