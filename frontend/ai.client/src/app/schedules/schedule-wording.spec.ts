import { describe, it, expect } from 'vitest';
import { WEEKDAY_LABELS, cadenceSummary } from './schedule-wording';

describe('cadenceSummary', () => {
  // `weekday` is the backend's 0=Monday..6=Sunday (`compute_next_run_at` in
  // apis/shared/scheduled_prompts/service.py). A schedule stored with 0 runs on
  // a Monday, so every summary must say Monday.
  it('names weekday 0 Monday and 6 Sunday', () => {
    expect(cadenceSummary({ cadence: 'weekly', hourLocal: 8, weekday: 0 })).toBe('Every Monday at 8:00 AM');
    expect(cadenceSummary({ cadence: 'weekly', hourLocal: 8, weekday: 6 })).toBe('Every Sunday at 8:00 AM');
  });

  it('names every day in 0=Monday..6=Sunday order', () => {
    const days = [0, 1, 2, 3, 4, 5, 6].map((weekday) =>
      cadenceSummary({ cadence: 'weekly', hourLocal: 9, weekday }).replace(/^Every (\w+) at .*$/, '$1'),
    );
    expect(days).toEqual(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']);
    expect(WEEKDAY_LABELS).toEqual(days);
  });

  it('words the other cadences', () => {
    expect(cadenceSummary({ cadence: 'daily', hourLocal: 7 })).toBe('Every day at 7:00 AM');
    expect(cadenceSummary({ cadence: 'weekday', hourLocal: 13 })).toBe('Every weekday at 1:00 PM');
    expect(cadenceSummary({ cadence: 'interval', hourLocal: 0, intervalValue: 6, intervalUnit: 'hours' })).toBe(
      'Every 6 hours',
    );
    expect(cadenceSummary({ cadence: 'interval', hourLocal: 0, intervalValue: 1, intervalUnit: 'hours' })).toBe(
      'Every hour',
    );
  });
});
