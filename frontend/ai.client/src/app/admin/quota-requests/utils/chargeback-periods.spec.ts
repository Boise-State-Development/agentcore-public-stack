import { describe, expect, it } from 'vitest';
import { closesAt, formatPeriod, lastCompleteMonth, rangeFor } from './chargeback-periods';

const NOW = new Date('2026-11-02T15:00:00Z');

describe('chargeback periods', () => {
  it('defaults to the last complete month, across a year boundary too', () => {
    expect(lastCompleteMonth(NOW)).toBe('2026-10');
    expect(lastCompleteMonth(new Date('2027-01-05T00:00:00Z'))).toBe('2026-12');
  });

  it('builds a month, a calendar quarter and a fiscal year around the anchor', () => {
    expect(rangeFor('month', '2026-08', '06-30', NOW)).toEqual({ from: '2026-08', to: '2026-08' });
    expect(rangeFor('quarter', '2026-08', '06-30', NOW)).toEqual({ from: '2026-07', to: '2026-09' });
    expect(rangeFor('fiscal_year', '2026-05', '06-30', NOW)).toEqual({ from: '2025-07', to: '2026-06' });
    expect(rangeFor('fiscal_year', '2025-07', '06-30', NOW)).toEqual({ from: '2025-07', to: '2026-06' });
    expect(rangeFor('fiscal_year', '2025-03', '12-31', NOW)).toEqual({ from: '2025-01', to: '2025-12' });
  });

  it('never runs past the current month', () => {
    expect(rangeFor('quarter', '2026-10', '12-31', NOW)).toEqual({ from: '2026-10', to: '2026-11' });
    expect(rangeFor('fiscal_year', '2026-10', '06-30', NOW)).toEqual({ from: '2026-07', to: '2026-11' });
  });

  it('closes 48 hours after the month ends and reads as a month name', () => {
    expect(closesAt('2026-10').toISOString()).toBe('2026-11-03T00:00:00.000Z');
    expect(closesAt('2026-12').toISOString()).toBe('2027-01-03T00:00:00.000Z');
    expect(formatPeriod('2026-10')).toBe('October 2026');
    expect(formatPeriod('2026-10', 'short')).toBe('Oct 2026');
  });
});
