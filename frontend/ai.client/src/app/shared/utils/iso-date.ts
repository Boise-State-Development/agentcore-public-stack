/**
 * Parsing helpers for backend timestamps.
 *
 * Much of the backend emits `datetime.now(timezone.utc).isoformat() + "Z"`, which
 * produces `2026-07-25T05:34:23.972451+00:00Z` — an offset *and* a `Z`. That is not
 * valid ISO 8601, and strict engines (Safari) refuse it outright while V8 returns
 * `Invalid Date`. The pattern is spread across ~80 call sites and is already persisted
 * in records, so consumers tolerate it rather than wait for a sweep.
 */

/** Parse a backend timestamp, stripping a redundant trailing `Z` after an offset. */
export function parseBackendIso(iso: string | undefined | null): Date | null {
  if (!iso) return null;
  const date = new Date(iso.replace(/([+-]\d{2}:\d{2})Z$/, '$1'));
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * "Jul 24" — a date the reader can act on, without a year they already know.
 *
 * Falls back to the year for anything older than ~10 months, so a stale record does
 * not silently read as recent. Returns `''` when the timestamp is unparseable, which
 * callers render as no date rather than as "Invalid Date".
 */
export function formatShortDate(iso: string | undefined | null): string {
  const date = parseBackendIso(iso);
  if (!date) return '';
  const monthsOld = (Date.now() - date.getTime()) / (30 * 86_400_000);
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(monthsOld > 10 ? { year: 'numeric' } : {}),
  });
}
