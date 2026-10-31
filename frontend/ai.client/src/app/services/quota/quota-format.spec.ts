import { describe, it, expect } from 'vitest';
import { endOfPeriod, endsMidMonth, formatAmount, formatQuotaDate, nextOccurrence, perPeriod } from './quota-format';

describe('quota-format', () => {
  const today = new Date('2026-10-31T23:30:00Z');

  it('ends a monthly period on the last UTC day of the month, a daily one today', () => {
    expect(endOfPeriod(today, 'monthly')).toBe('2026-10-31');
    expect(endOfPeriod(new Date('2026-02-03T00:00:00Z'), 'monthly')).toBe('2026-02-28');
    expect(endOfPeriod(today, 'daily')).toBe('2026-10-31');
  });

  it('finds the next fiscal year end on or after today', () => {
    expect(nextOccurrence(today, '06-30')).toBe('2027-06-30');
    expect(nextOccurrence(today, '12-31')).toBe('2026-12-31');
  });

  it('knows a mid-month end from a month end', () => {
    expect(endsMidMonth('2026-11-15')).toBe(true);
    expect(endsMidMonth('2026-11-30')).toBe(false);
  });

  it('formats dates in UTC and amounts as people say them', () => {
    expect(formatQuotaDate('2026-11-30')).toBe('Nov 30, 2026');
    expect(formatQuotaDate('2026-11-30T23:59:59+00:00')).toBe('Nov 30, 2026');
    expect(formatAmount(25)).toBe('$25');
    expect(formatAmount(12.5)).toBe('$12.50');
    expect(perPeriod('daily')).toBe('per day');
    expect(perPeriod('monthly')).toBe('per month');
  });
});
