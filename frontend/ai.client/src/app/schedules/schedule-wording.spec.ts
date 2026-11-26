import { describe, it, expect } from 'vitest';
import { WEEKDAY_LABELS, cadenceSummary, lastDayRunSummary, pausedRunLabel, runStatusLabel, runsPerDay } from './schedule-wording';

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
    expect(runStatusLabel('needs_user')).toBe('Waiting for you');
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

describe('pausedRunLabel', () => {
  const now = Date.parse('2026-11-24T22:40:00Z');
  const tz = (iso: string) => new Date(iso).toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' });

  it('says when a waiting run expires: the weekday within the week, else the date', () => {
    const soon = '2026-11-25T22:40:00Z';
    expect(pausedRunLabel({ expiresAt: soon }, now)).toBe(`Waiting for you · expires ${tz(soon)}`);
    const far = '2026-12-01T22:40:00Z';
    const date = new Date(far).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    expect(pausedRunLabel({ expiresAt: far }, now)).toBe(`Waiting for you · expires ${date}`);
    expect(pausedRunLabel({}, now)).toBe('Waiting for you');
  });

  it('reads Expired once past, derived on read', () => {
    expect(pausedRunLabel({ expiresAt: '2026-11-24T22:39:00Z' }, now)).toBe('Expired');
  });

  it.each([
    [{ resolution: 'approved', resumeOutcome: 'finished' }, 'Approved · finished'],
    [{ resolution: 'declined', resumeOutcome: 'finished' }, 'Declined · finished'],
    [{ resolution: 'answered', resumeOutcome: 'failed' }, 'Answered · failed'],
    [{ resolution: 'approved', resumeOutcome: 'waiting_again' }, 'Approved · waiting for you again'],
    [{ resolution: 'answered_elsewhere' }, 'Answered in the conversation'],
    // An answer outranks an expiry that has since passed.
    [{ resolution: 'skipped', resumeOutcome: 'finished', expiresAt: '2026-01-01T00:00:00Z' }, 'Skipped · finished'],
  ])('%o reads as “%s”', (run, text) => {
    expect(pausedRunLabel(run, now)).toBe(text);
    expect(runStatusLabel('needs_user', 0, run, now)).toBe(text);
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

  it('counts a run that waits on the user as a finished run, never a failure', () => {
    // answer-from-inbox §3.8: it did its work. It is counted as waiting, never as a finding too.
    expect(lastDayRunSummary([run('needs_user', 1), run('quiet', 2)], true, now)).toBe(
      'Checked 2 times · found something 0 times · 1 waiting for you',
    );
    expect(lastDayRunSummary([run('needs_user', 1)], false, now)).toBe('Ran 1 time · 1 waiting for you');
  });

  it('adds the waiting tail only for a run still waiting: not one answered or expired', () => {
    const waiting = { ...run('needs_user', 1), expiresAt: new Date(now + 86_400_000).toISOString() };
    const answered = { ...run('needs_user', 2), resolution: 'approved', resumeOutcome: 'finished' };
    const lapsed = { ...run('needs_user', 3), expiresAt: new Date(now - 60_000).toISOString() };
    expect(lastDayRunSummary([waiting, answered, lapsed, run('error', 4)], true, now)).toBe(
      'Checked 3 times · found something 0 times · 1 failed · 1 waiting for you',
    );
    expect(lastDayRunSummary([answered, lapsed], false, now)).toBe('Ran 2 times');
    // Answered in its conversation: the bell's list marks the history row, which settles it.
    const elsewhere = { ...run('needs_user', 5), resolution: 'answered_elsewhere' };
    expect(lastDayRunSummary([elsewhere], true, now)).toBe('Checked 1 time · found something 0 times');
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
