/**
 * How a schedule is described in words, wherever it appears: the personal
 * `/schedules` page, a project's Schedules dialog, the notification bell and a
 * project's Activity. One copy, so the bell and the dialog never disagree about
 * why a schedule stopped.
 */

import { parseIso } from '../utils/date';
import { IntervalUnit, ScheduleCadence } from './models/schedule.model';

const WEEKDAY_LABELS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/** The fields a cadence summary reads. */
export interface ScheduleCadenceFields {
  cadence: ScheduleCadence;
  hourLocal: number;
  weekday?: number | null;
  intervalValue?: number | null;
  intervalUnit?: IntervalUnit | null;
}

/** "7:00 AM", "12:00 PM". */
export function formatHour(hour: number): string {
  if (hour === 0) return '12:00 AM';
  if (hour < 12) return `${hour}:00 AM`;
  if (hour === 12) return '12:00 PM';
  return `${hour - 12}:00 PM`;
}

/** "Every day at 7:00 AM", "Every Monday at 9:00 AM", "Every 6 hours". */
export function cadenceSummary(schedule: ScheduleCadenceFields): string {
  const time = formatHour(schedule.hourLocal);
  switch (schedule.cadence) {
    case 'daily':
      return `Every day at ${time}`;
    case 'weekday':
      return `Every weekday at ${time}`;
    case 'weekly': {
      const day = schedule.weekday !== null && schedule.weekday !== undefined ? WEEKDAY_LABELS[schedule.weekday] : '';
      return `Every ${day} at ${time}`;
    }
    case 'interval': {
      const value = schedule.intervalValue ?? 0;
      const unit = schedule.intervalUnit ?? 'hours';
      const singular = value === 1 ? unit.replace(/s$/, '') : unit;
      return value === 1 ? `Every ${singular}` : `Every ${value} ${singular}`;
    }
    default:
      return time;
  }
}

/**
 * Why a project schedule stopped, worded for its creator (`mine`) and for anyone
 * else (`theirs`), by the backend's `stateReason` code (`apis/shared/projects/schedules.py`
 * and the scheduled-runs worker). Each phrase follows "stopped".
 */
export const SCHEDULE_STOPS: Record<string, { mine: string; theirs: string }> = {
  member_removed: { mine: 'because you’re no longer a member', theirs: 'because its creator is no longer a member' },
  not_editor: { mine: 'because you’re no longer an editor', theirs: 'because its creator is no longer an editor' },
  project_archived: { mine: 'because the project was archived', theirs: 'because the project was archived' },
  project_deleted: { mine: 'because the project was deleted', theirs: 'because the project was deleted' },
  projects_disabled: { mine: 'because Projects is switched off', theirs: 'because Projects is switched off' },
  repeated_failures: { mine: 'after failing several times in a row', theirs: 'after failing several times in a row' },
  reauth_required: { mine: 'until you sign in again', theirs: 'until its creator signs in again' },
  oauth_required: { mine: 'until you reconnect a tool it uses', theirs: 'until its creator reconnects a tool it uses' },
  max_runs_per_day_exceeded: { mine: 'after reaching its daily run limit', theirs: 'after reaching its daily run limit' },
};

/**
 * The sentence for a stopped schedule's reason, or null when there is nothing to
 * say: an active schedule, a plain pause by its creator, or a code this build
 * doesn't know.
 */
export function scheduleStopSentence(reason: string | null | undefined, mine: boolean): string | null {
  if (!reason) return null;
  if (reason === 'paused_by_editor') return mine ? 'An editor paused your schedule.' : 'An editor paused it.';
  const stop = SCHEDULE_STOPS[reason];
  return stop ? `Stopped ${mine ? stop.mine : stop.theirs}.` : null;
}

/** "in 5m", "in 3h", "in 2d", "due now"; "—" for an unreadable time. */
export function relativeFuture(iso: string, now = Date.now()): string {
  const then = parseIso(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const diffMins = Math.round((then - now) / 60_000);
  if (diffMins <= 0) return 'due now';
  if (diffMins < 60) return `in ${diffMins}m`;
  const diffHours = Math.round(diffMins / 60);
  if (diffHours < 24) return `in ${diffHours}h`;
  return `in ${Math.round(diffHours / 24)}d`;
}

/** "just now", "5m ago", "3h ago", "2d ago"; empty for an unreadable time. */
export function relativePast(iso: string, now = Date.now()): string {
  const then = parseIso(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diffMins = Math.floor((now - then) / 60_000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${Math.floor(diffHours / 24)}d ago`;
}

/** A run's outcome as a word: the worker's `completed` / `error` / `timeout`, and a recorded pause. */
export function runStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'completed':
      return 'Completed';
    case 'error':
      return 'Failed';
    case 'timeout':
      return 'Timed out';
    case 'paused':
      return 'Paused';
    default:
      return status ? status.charAt(0).toUpperCase() + status.slice(1) : 'Unknown';
  }
}
