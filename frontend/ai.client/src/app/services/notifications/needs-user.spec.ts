import { describe, it, expect } from 'vitest';
import { AppNotification } from './notification.model';
import { decisionLine, expiresIn, needsUserReviewable, needsUserWaiting } from './needs-user';

const NOW = Date.parse('2026-11-26T12:00:00Z');
const at = (ms: number) => new Date(NOW + ms).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

function row(over: Partial<AppNotification> = {}, payload: Partial<AppNotification['payload']> = {}): AppNotification {
  return {
    notificationId: 'n1', recipientEmail: 'me@x.edu', kind: 'agent_needs_approval',
    payload: { sessionId: 'headless-1', label: 'Watcher', expiresAt: at(DAY), answerable: true, prompts: [], ...payload },
    createdAt: '2026-11-26T00:00:00Z', readAt: null, ...over,
  };
}

describe('expiresIn', () => {
  it.each([
    [at(6 * DAY + 5 * HOUR), 'Expires in 6 days'],
    [at(DAY), 'Expires in 1 day'],
    [at(3 * HOUR + 10 * MIN), 'Expires in 3 hours'],
    [at(HOUR), 'Expires in 1 hour'],
    [at(12 * MIN), 'Expires in 12 minutes'],
    [at(30_000), 'Expires in under a minute'],
    [at(-MIN), 'Expired'],
    ['not a date', ''],
    [undefined, ''],
  ])('%s reads as “%s”', (iso, text) => {
    expect(expiresIn(iso, NOW)).toBe(text);
  });
});

describe('decisionLine', () => {
  it.each([
    ['approved', 'finished', 'Approved · finished'],
    ['declined', 'finished', 'Declined · finished'],
    ['answered', 'failed', 'Answered · failed'],
    ['approved', 'waiting_again', 'Approved · waiting for you again'],
    ['skipped', null, 'Skipped'],
    [null, null, 'Answered'],
  ])('%s + %s reads as “%s”', (decision, outcome, text) => {
    expect(decisionLine(decision, outcome)).toBe(text);
  });
});

describe('needsUserReviewable', () => {
  it('is a waiting row whose every prompt has a card here', () => {
    expect(needsUserReviewable(row(), NOW)).toBe(true);
    expect(needsUserReviewable(row({ resolution: { state: 'retry', at: at(0) } }), NOW)).toBe(true);
  });

  it('is never one waiting on a sign-in, settled, lapsed, or another kind', () => {
    expect(needsUserReviewable(row({}, { answerable: false }), NOW)).toBe(false);
    expect(needsUserReviewable(row({ resolution: { state: 'applying', at: at(0) } }), NOW)).toBe(false);
    expect(needsUserReviewable(row({}, { expiresAt: at(-MIN) }), NOW)).toBe(false);
    expect(needsUserReviewable(row({ kind: 'agent_item' }), NOW)).toBe(false);
    expect(needsUserWaiting(row({}, { answerable: false }), NOW)).toBe(true);
  });
});
