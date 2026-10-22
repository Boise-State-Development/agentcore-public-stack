/**
 * Date parsing that tolerates the legacy `…+00:00Z` timestamps still in the database.
 *
 * A tz-aware `datetime.now(timezone.utc).isoformat()` already renders the offset as
 * `+00:00`; appending `"Z"` produced `2026-07-27T05:09:55.853557+00:00Z` — an offset *and*
 * a Z — which is not valid ISO 8601. `new Date()` returns `Invalid Date` for it, so every
 * surface that formatted such a value silently showed a placeholder: the agent detail page
 * rendered "Last updated —" on an agent edited minutes earlier, and the admin Reports queue
 * rendered "recently" for every report ever filed.
 *
 * The backend writers are fixed (`apis.shared.timestamps`), but rows written before that
 * deploy keep the bad spelling forever — `createdAt` in particular is never rewritten. We
 * normalize on read rather than backfilling, because those values are embedded in GSI sort
 * keys (`GSI5_SK = CREATED#{created_at}`) and rewriting them is far riskier than tolerating
 * two spellings here.
 *
 * ⚠️ **Use `parseIso` instead of `new Date(...)` for anything that came from the API.**
 * It is a drop-in: non-string inputs (epoch millis, an existing `Date`) pass straight
 * through, so it is safe on mixed call sites.
 */

/** Repair a legacy `+00:00Z` timestamp. A no-op for already-valid values. */
export function normalizeIsoTimestamp(value: string): string {
  return value.replace('+00:00Z', 'Z');
}

/**
 * `new Date(...)`, but tolerant of the legacy timestamp spelling.
 *
 * Returns an Invalid Date for genuinely unparseable input, exactly as `new Date` would —
 * callers that already guard with `Number.isNaN(d.getTime())` keep working unchanged.
 */
export function parseIso(value: string | number | Date | null | undefined): Date {
  if (value instanceof Date) return value;
  if (typeof value === 'number') return new Date(value);
  if (value === null || value === undefined) return new Date(NaN);
  return new Date(normalizeIsoTimestamp(value));
}

/** Matches a whole value in the legacy spelling, so free text that merely contains it is left alone. */
const LEGACY_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00Z$/;

/**
 * Deep-copy an API payload with every legacy `…+00:00Z` timestamp repaired.
 *
 * For surfaces that hand API values straight to Angular's `DatePipe`. Unlike `new Date`,
 * which degrades to `Invalid Date`, `DatePipe` **throws** (`NG02100`) on the legacy form —
 * and a throw mid-template aborts the change-detection pass, leaving later bindings
 * unrendered until some unrelated event triggers another pass. Normalizing once at the
 * HTTP boundary fixes every `| date` downstream without touching each template.
 */
export function normalizeLegacyTimestamps<T>(value: T): T {
  if (typeof value === 'string') {
    return (LEGACY_TIMESTAMP.test(value) ? normalizeIsoTimestamp(value) : value) as T;
  }
  if (Array.isArray(value)) {
    return value.map(normalizeLegacyTimestamps) as T;
  }
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, v] of Object.entries(value)) out[key] = normalizeLegacyTimestamps(v);
    return out as T;
  }
  return value;
}
