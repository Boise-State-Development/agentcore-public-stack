/**
 * How a paused scheduled run's inbox row reads (answer-from-inbox §3.3, §3.8): whether it still
 * waits, when it expires, and the words for what became of it. One copy, so the bell, the review
 * dialog and the background task's toast never disagree.
 */

import { parseIso } from '../../utils/date';
import { AppNotification, NEEDS_USER_NOTIFICATION_KINDS } from './notification.model';

/** Whether a needs-user row's pause has passed its expiry, judged on this clock (§3.8). */
export function needsUserExpired(n: AppNotification, now = Date.now()): boolean {
  if (n.resolution?.state === 'expired') return true;
  if (n.resolution) return false;
  const expiresAt = typeof n.payload?.expiresAt === 'string' ? parseIso(n.payload.expiresAt).getTime() : Number.NaN;
  return !Number.isNaN(expiresAt) && expiresAt <= now;
}

/**
 * A needs-user row still waiting on the user: not settled (or given back to `retry`) and not past
 * its expiry. Its read state follows the answer, not the click (§3.3): opening it leaves it
 * unread, and the server marks it read once it is answered, here or in the conversation, or
 * expires.
 */
export function needsUserWaiting(n: AppNotification, now = Date.now()): boolean {
  if (!NEEDS_USER_NOTIFICATION_KINDS.has(n.kind)) return false;
  if (n.resolution && n.resolution.state !== 'retry') return false;
  return !needsUserExpired(n, now);
}

/** A waiting row the review dialog can answer: every prompt has a card here (§3.3 `answerable`). */
export function needsUserReviewable(n: AppNotification, now = Date.now()): boolean {
  return needsUserWaiting(n, now) && n.payload?.answerable === true;
}

function plural(count: number, unit: string): string {
  return count === 1 ? `1 ${unit}` : `${count} ${unit}s`;
}

/**
 * "Expires in 6 days", then "in 3 hours", "in 12 minutes" (§3.8's bell row). "Expired" once past;
 * empty for a time this build can't read.
 */
export function expiresIn(iso: string | null | undefined, now = Date.now()): string {
  const then = typeof iso === 'string' ? parseIso(iso).getTime() : Number.NaN;
  if (Number.isNaN(then)) return '';
  const mins = Math.floor((then - now) / 60_000);
  if (then <= now) return 'Expired';
  if (mins < 1) return 'Expires in under a minute';
  if (mins < 60) return `Expires in ${plural(mins, 'minute')}`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `Expires in ${plural(hours, 'hour')}`;
  return `Expires in ${plural(Math.floor(hours / 24), 'day')}`;
}

/** The decision a resume carried, as the row and the history say it. */
export const DECISION_WORDS: Readonly<Record<string, string>> = {
  approved: 'Approved',
  declined: 'Declined',
  answered: 'Answered',
  skipped: 'Skipped',
};

/** What the resumed run then did. */
export const OUTCOME_WORDS: Readonly<Record<string, string>> = {
  finished: 'finished',
  waiting_again: 'waiting for you again',
  failed: 'failed',
};

/** "Approved · finished", "Answered · failed"; the decision alone while the run has no outcome yet. */
export function decisionLine(decision: string | null | undefined, outcome: string | null | undefined): string {
  const word = DECISION_WORDS[decision ?? ''] ?? 'Answered';
  const then = OUTCOME_WORDS[outcome ?? ''];
  return then ? `${word} · ${then}` : word;
}
