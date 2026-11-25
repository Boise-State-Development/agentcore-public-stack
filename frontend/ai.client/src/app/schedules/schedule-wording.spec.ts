import { describe, it, expect } from 'vitest';
import { WEEKDAY_LABELS, cadenceSummary, lastDayRunSummary, runStatusLabel, runsPerDay } from './schedule-wording';

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

describe('runStatusLabel', () => {
  it('words the standard outcomes', () => {
    expect(runStatusLabel('completed')).toBe('Completed');
    expect(runStatusLabel('error')).toBe('Failed');
    expect(runStatusLabel('timeout')).toBe('Timed out');
    expect(runStatusLabel('paused')).toBe('Paused');
  });

  it('words a watcher’s quiet and notify runs, counting what it found', () => {
    expect(runStatusLabel('quiet')).toBe('Nothing new');
    expect(runStatusLabel('notify', 3)).toBe('Found 3');
    expect(runStatusLabel('notify', 1)).toBe('Found 1');
    expect(runStatusLabel('notify', 0)).toBe('Found something');
    expect(runStatusLabel('notify')).toBe('Found something');
  });

  it('capitalizes a status this build doesn’t know', () => {
    expect(runStatusLabel('skipped')).toBe('Skipped');
    expect(runStatusLabel(null)).toBe('Unknown');
  });
});

describe('lastDayRunSummary', () => {
  const now = Date.parse('2026-11-25T12:00:00Z');
  const run = (status: string, hoursAgo: number) => ({
    status,
    finishedAt: new Date(now - hoursAgo * 3_600_000).toISOString(),
  });

  it('counts a watcher’s checks and findings in the last 24 hours only', () => {
    const runs = [run('quiet', 1), run('notify', 2), run('quiet', 3), run('notify', 23), run('notify', 30)];
    expect(lastDayRunSummary(runs, true, now)).toBe('Checked 4 times · found something 2 times');
    expect(lastDayRunSummary([run('notify', 1)], true, now)).toBe('Checked 1 time · found something 1 time');
  });

  it('never counts a run that stopped before it started as a check', () => {
    // Seen on dev: a run paused for expired access read "Checked 1 time".
    expect(lastDayRunSummary([run('paused', 1)], true, now)).toBe('Checked 0 times · found something 0 times · 1 failed');
    expect(lastDayRunSummary([run('notify', 1), run('error', 2), run('timeout', 3)], true, now)).toBe(
      'Checked 1 time · found something 1 time · 2 failed',
    );
  });

  it('counts a standard schedule’s runs, and says when there were none', () => {
    expect(lastDayRunSummary([run('completed', 2), run('error', 5)], false, now)).toBe('Ran 1 time · 1 failed');
    expect(lastDayRunSummary([run('completed', 2)], false, now)).toBe('Ran 1 time');
    expect(lastDayRunSummary([run('completed', 25)], false, now)).toBe('No runs in the last 24 hours');
    expect(lastDayRunSummary([], true, now)).toBe('No runs in the last 24 hours');
  });
});

describe('runsPerDay', () => {
  it('is the whole runs an interval makes in a day', () => {
    expect(runsPerDay(15)).toBe(96);
    expect(runsPerDay(45)).toBe(32);
    expect(runsPerDay(0)).toBe(0);
  });
});
