import { describe, expect, it } from 'vitest';
import { normalizeIsoTimestamp, normalizeLegacyTimestamps, parseIso } from './date';

/**
 * The legacy spelling still sitting in DynamoDB. Rows written before the backend fix keep
 * it forever — `createdAt` is never rewritten — so the SPA has to tolerate it rather than
 * rely on a backfill.
 */
const LEGACY = '2026-07-27T05:09:55.853557+00:00Z';
const VALID = '2026-07-27T05:09:55.853557Z';

describe('parseIso', () => {
  it('is the bug: plain `new Date` cannot parse the legacy form', () => {
    // Guards the guard — if this ever starts passing, these tests prove nothing.
    expect(Number.isNaN(new Date(LEGACY).getTime())).toBe(true);
  });

  it('parses the legacy +00:00Z form to the correct instant', () => {
    expect(parseIso(LEGACY).toISOString()).toBe(new Date(VALID).toISOString());
  });

  it('leaves an already-valid timestamp alone', () => {
    expect(parseIso(VALID).toISOString()).toBe(new Date(VALID).toISOString());
  });

  it('passes epoch millis through, so mixed call sites stay safe', () => {
    expect(parseIso(1_785_160_195_000).getTime()).toBe(1_785_160_195_000);
  });

  it('passes an existing Date through untouched', () => {
    const d = new Date(VALID);
    expect(parseIso(d)).toBe(d);
  });

  it('returns an Invalid Date for null/undefined rather than throwing', () => {
    // Callers already branch on Number.isNaN(getTime()); preserve that contract.
    expect(Number.isNaN(parseIso(null).getTime())).toBe(true);
    expect(Number.isNaN(parseIso(undefined).getTime())).toBe(true);
  });

  it('still yields an Invalid Date for genuinely unparseable input', () => {
    expect(Number.isNaN(parseIso('not a date').getTime())).toBe(true);
  });
});

describe('normalizeLegacyTimestamps', () => {
  it('repairs legacy values at any depth and leaves everything else as-is', () => {
    const input = {
      createdAt: LEGACY,
      items: [{ at: LEGACY, n: 3, ok: true, none: null }],
      note: `free text mentioning ${LEGACY}`,
      valid: VALID,
    };
    expect(normalizeLegacyTimestamps(input)).toEqual({
      createdAt: VALID,
      items: [{ at: VALID, n: 3, ok: true, none: null }],
      note: `free text mentioning ${LEGACY}`,
      valid: VALID,
    });
  });

  it('does not mutate its input', () => {
    const input = { createdAt: LEGACY };
    normalizeLegacyTimestamps(input);
    expect(input.createdAt).toBe(LEGACY);
  });

  it('passes primitives and null through', () => {
    expect(normalizeLegacyTimestamps(null)).toBeNull();
    expect(normalizeLegacyTimestamps(42)).toBe(42);
    expect(normalizeLegacyTimestamps(LEGACY)).toBe(VALID);
  });
});

describe('normalizeIsoTimestamp', () => {
  it('rewrites the doubled offset', () => {
    expect(normalizeIsoTimestamp(LEGACY)).toBe(VALID);
  });

  it('is a no-op on a valid value', () => {
    expect(normalizeIsoTimestamp(VALID)).toBe(VALID);
  });

  it('leaves a genuine non-UTC offset intact', () => {
    // Only the `+00:00Z` pair is wrong; a real offset must survive.
    expect(normalizeIsoTimestamp('2026-07-27T00:09:55-05:00')).toBe(
      '2026-07-27T00:09:55-05:00',
    );
  });
});
