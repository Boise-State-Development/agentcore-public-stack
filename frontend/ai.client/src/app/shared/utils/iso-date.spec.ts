import { describe, it, expect, vi, afterEach } from 'vitest';
import { parseBackendIso, formatShortDate } from './iso-date';

describe('parseBackendIso', () => {
  it('parses a well-formed UTC timestamp', () => {
    expect(parseBackendIso('2026-07-24T10:00:00Z')?.toISOString()).toBe('2026-07-24T10:00:00.000Z');
  });

  it('tolerates the backend form that carries both an offset and a Z', () => {
    // `datetime.now(timezone.utc).isoformat() + "Z"` — invalid ISO 8601, already
    // persisted across ~80 call sites, so consumers strip rather than wait for a sweep.
    expect(parseBackendIso('2026-07-24T10:00:00.972451+00:00Z')?.toISOString()).toBe(
      '2026-07-24T10:00:00.972Z',
    );
  });

  it('preserves a real non-UTC offset', () => {
    expect(parseBackendIso('2026-07-24T10:00:00-06:00')?.toISOString()).toBe(
      '2026-07-24T16:00:00.000Z',
    );
  });

  it('returns null rather than an Invalid Date', () => {
    expect(parseBackendIso('not-a-date')).toBeNull();
    expect(parseBackendIso(undefined)).toBeNull();
    expect(parseBackendIso('')).toBeNull();
  });
});

describe('formatShortDate', () => {
  afterEach(() => vi.useRealTimers());

  it('omits the year for a recent date', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-25T00:00:00Z'));
    expect(formatShortDate('2026-07-24T18:00:00Z')).not.toMatch(/2026/);
  });

  it('adds the year once a date is old enough to mislead', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-25T00:00:00Z'));
    expect(formatShortDate('2025-01-10T10:00:00Z')).toMatch(/2025/);
  });

  it('renders nothing for an unparseable timestamp', () => {
    expect(formatShortDate('not-a-date')).toBe('');
  });
});
