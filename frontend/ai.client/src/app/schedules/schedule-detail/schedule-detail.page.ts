import { ChangeDetectionStrategy, Component, computed, effect, inject, input, resource, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { ScheduleService } from '../services/schedule.service';
import { ScheduleApiService } from '../services/schedule-api.service';
import { ScheduleRun, ScheduledPrompt, WatchState } from '../models/schedule.model';
import { ModelService } from '../../session/services/model/model.service';
import { ToastService } from '../../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { parseIso } from '../../utils/date';
import { cadenceSummary, lastDayRunSummary, relativePast, runStatusLabel, runStillWaiting } from '../schedule-wording';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { AppNotification, NEEDS_USER_NOTIFICATION_KINDS } from '../../services/notifications/notification.model';
import { needsUserReviewable, needsUserWaiting } from '../../services/notifications/needs-user';
import { InboxAnswerService } from '../services/inbox-answer.service';

/** "950", "12.3k", "1.2M". */
export function compactTokens(count: number): string {
  if (!Number.isFinite(count) || count < 1000) return String(Math.max(0, Math.round(count || 0)));
  if (count < 1_000_000) return `${trimZero((count / 1000).toFixed(1))}k`;
  return `${trimZero((count / 1_000_000).toFixed(1))}M`;
}

/**
 * A run's tokens, for its history row: "Tokens: 8 in · 180 out · 4.5k written
 * to cache · 4.3k read from cache". The cache write is named on its own because
 * it bills at a premium and is most of a short run's cost. Empty for a run that
 * used no tokens (one that stopped before it started), so the row says nothing.
 */
export function runTokens(run: ScheduleRun): string {
  const write = run.cacheWriteTokens ?? 0;
  if (!run.inputTokens && !run.outputTokens && !run.cacheReadTokens && !write) return '';
  const parts = [`${compactTokens(run.inputTokens)} in`, `${compactTokens(run.outputTokens)} out`];
  if (write) parts.push(`${compactTokens(write)} written to cache`);
  if (run.cacheReadTokens) parts.push(`${compactTokens(run.cacheReadTokens)} read from cache`);
  return `Tokens: ${parts.join(' · ')}`;
}

function trimZero(value: string): string {
  return value.endsWith('.0') ? value.slice(0, -2) : value;
}

/** The HTTP status behind a failed load; `resource()` may wrap the loader's error. */
function statusOf(error: unknown): number | undefined {
  const direct = (error as { status?: number } | null)?.status;
  if (typeof direct === 'number') return direct;
  return (error as { cause?: { status?: number } } | null)?.cause?.status;
}

/** One run as the history list shows it. */
interface RunRow {
  run: ScheduleRun;
  label: string;
  when: string;
  absolute: string;
  tokens: string;
  badgeClass: string;
}

const BADGE = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';

/** A watcher holding on a run that waits on the user (answer-from-inbox §3.8). */
export interface WatchHold {
  sessionId: string;
  /** "Waiting for you since 9:15 AM", or without a time when the run isn't in the history. */
  sentence: string;
  /** The inbox row to review it from, when the bell has it and it can be answered there. */
  notification: AppNotification | null;
}

/** "9:15 AM" today, "Mon 9:15 AM" earlier. */
function sinceMoment(at: Date, now: number): string {
  const sameDay = new Date(now).toDateString() === at.toDateString();
  return sameDay
    ? at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
    : at.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' });
}

/**
 * The banner's sentence and review target, or null when the watcher isn't holding: no pause on
 * its state, one past `pausedUntil`, or one the inbox already shows as settled (answered in the
 * conversation, which the next tick clears from the state).
 */
export function watchHold(
  state: WatchState | null,
  runs: readonly ScheduleRun[],
  inbox: readonly AppNotification[],
  now = Date.now(),
): WatchHold | null {
  const sessionId = state?.pausedSessionId;
  const until = state?.pausedUntil ? parseIso(state.pausedUntil) : null;
  if (!sessionId || !until || Number.isNaN(until.getTime()) || until.getTime() <= now) return null;
  const rows = inbox.filter((n) => NEEDS_USER_NOTIFICATION_KINDS.has(n.kind) && n.payload?.sessionId === sessionId);
  if (rows.length > 0 && !rows.some((n) => needsUserWaiting(n, now))) return null;
  const paused = runs
    .filter((run) => run.status === 'needs_user' && run.sessionId === sessionId)
    .sort((a, b) => parseIso(b.finishedAt).getTime() - parseIso(a.finishedAt).getTime())[0];
  const pausedAt = paused ? parseIso(paused.finishedAt) : null;
  const since = pausedAt && !Number.isNaN(pausedAt.getTime()) ? ` since ${sinceMoment(pausedAt, now)}` : '';
  const day = until.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return {
    sessionId,
    sentence: `Waiting for you${since}. Checks resume once you answer, or on ${day} when it expires.`,
    notification: rows.find((n) => needsUserReviewable(n, now)) ?? null,
  };
}

/**
 * One personal schedule (docs/specs/quiet-watcher.md §1, §3.4): what it is, every
 * run it made in the last 30 days (quiet ones included, which leave nothing else
 * behind), and for a watcher what it is tracking, with Start over.
 *
 * Routed at `schedules/:scheduleId` behind `features.proactiveRuns`; the run and
 * state endpoints 404 while the backend's PROACTIVE_RUNS_ENABLED is off. The data
 * belongs to this view and should be fresh on every visit, so the resources live
 * here rather than on a root service.
 */
@Component({
  selector: 'app-schedule-detail-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  providers: [provideIcons({ heroArrowLeft })],
  templateUrl: './schedule-detail.page.html',
})
export class ScheduleDetailPage {
  private readonly scheduleService = inject(ScheduleService);
  private readonly api = inject(ScheduleApiService);
  private readonly models = inject(ModelService);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);
  private readonly inbox = inject(NotificationsService);
  private readonly inboxAnswer = inject(InboxAnswerService);

  /** From the route (component input binding). */
  readonly scheduleId = input.required<string>();

  readonly schedule = resource({
    params: () => ({ id: this.scheduleId() }),
    loader: async ({ params }) => {
      const found = await this.scheduleService.getSchedule(params.id);
      if (!found) throw Object.assign(new Error('Schedule not found'), { status: 404 });
      return found;
    },
  });

  /**
   * The schedule once loaded, if it is a personal one: a project schedule's runs
   * are on its project, and asking here would only 404.
   */
  private readonly personal = computed<ScheduledPrompt | null>(() => {
    const schedule = this.schedule.hasValue() ? this.schedule.value() : null;
    return schedule && !schedule.projectId ? schedule : null;
  });

  readonly watching = computed(() => this.personal()?.mode === 'watch');

  readonly runs = resource({
    params: () => {
      const schedule = this.personal();
      return schedule ? { id: schedule.scheduleId } : undefined;
    },
    loader: ({ params }) => firstValueFrom(this.api.runs(params.id)),
  });

  readonly watchState = resource({
    params: () => {
      const schedule = this.personal();
      return schedule && schedule.mode === 'watch' ? { id: schedule.scheduleId } : undefined;
    },
    loader: ({ params }) => firstValueFrom(this.api.watchState(params.id)),
  });

  readonly clearing = signal(false);

  readonly notFound = computed(() => !!this.schedule.error() && statusOf(this.schedule.error()) === 404);
  readonly isProjectSchedule = computed(() => this.schedule.hasValue() && !!this.schedule.value().projectId);

  readonly cadence = computed(() => {
    const schedule = this.schedule.hasValue() ? this.schedule.value() : null;
    return schedule ? `${cadenceSummary(schedule)} · ${schedule.timezone}` : '';
  });

  readonly modeLabel = computed(() =>
    this.watching() ? 'Watches, and only tells you when something is new' : 'Runs and delivers every time',
  );

  readonly modelLabel = computed(() => {
    const id = this.schedule.hasValue() ? this.schedule.value().modelId : null;
    if (!id) return 'Default';
    return this.models.modelNameFor(id) ?? id;
  });

  /** Newest first, whatever order the server sent. */
  readonly runRows = computed<RunRow[]>(() => {
    const runs = this.runs.hasValue() ? this.runs.value().runs : [];
    return [...runs]
      .sort((a, b) => parseIso(b.finishedAt).getTime() - parseIso(a.finishedAt).getTime())
      .map((run) => ({
        run,
        label: runStatusLabel(run.status, run.itemCount, run),
        when: relativePast(run.finishedAt),
        absolute: parseIso(run.finishedAt).toLocaleString(),
        tokens: runTokens(run),
        badgeClass: this.badgeClass(run),
      }));
  });

  readonly daySummary = computed(() =>
    lastDayRunSummary(this.runs.hasValue() ? this.runs.value().runs : [], this.watching()),
  );

  readonly state = computed<WatchState | null>(() =>
    this.watchState.hasValue() ? this.watchState.value().state : null,
  );
  readonly cursors = computed(() => Object.entries(this.state()?.cursors ?? {}).sort(([a], [b]) => a.localeCompare(b)));
  readonly tracking = computed(() => this.state()?.tracking ?? []);
  readonly seenCount = computed(() => this.state()?.seen?.length ?? 0);
  /** Whether there is anything to show (or clear): a state row with something in it. */
  readonly hasState = computed(
    () => this.cursors().length > 0 || this.tracking().length > 0 || this.seenCount() > 0,
  );
  readonly seenSentence = computed(() => {
    const count = this.seenCount();
    if (count === 0) return 'It hasn’t told you about anything yet.';
    const findings = count === 1 ? '1 finding' : `${count} findings`;
    return `It remembers ${findings} it has already told you about, so it won’t tell you again.`;
  });

  /** The banner while a watcher holds on a run that waits on the user (§3.8). */
  readonly hold = computed(() =>
    this.watching()
      ? watchHold(this.state(), this.runs.hasValue() ? this.runs.value().runs : [], this.inbox.notifications())
      : null,
  );

  readonly skeletonRows = [0, 1, 2];

  constructor() {
    // A resume this tab started settled: its run's history row and the watcher's hold changed.
    let first = true;
    effect(() => {
      this.inboxAnswer.settledCount();
      if (first) {
        first = false;
        return;
      }
      untracked(() => {
        if (this.runs.hasValue()) this.runs.reload();
        if (this.watchState.hasValue()) this.watchState.reload();
      });
    });
  }

  /** Review the held run's prompts from here, as from the bell. */
  review(hold: WatchHold): void {
    if (hold.notification) void this.inboxAnswer.review(hold.notification);
  }

  async startOver(): Promise<void> {
    const schedule = this.personal();
    if (!schedule || this.clearing()) return;
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Start over?',
        message:
          `“${schedule.label}” will forget what it’s tracking and what it has already told you about. ` +
          'Its next run treats everything it finds as new.',
        confirmText: 'Start over',
        cancelText: 'Cancel',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    const confirmed = await firstValueFrom(ref.closed);
    if (!confirmed) return;

    this.clearing.set(true);
    try {
      await firstValueFrom(this.api.clearWatchState(schedule.scheduleId));
      this.watchState.reload();
      this.toast.success('Started over', 'Its next run treats everything as new.');
    } catch {
      this.toast.error('Could not start over', 'Please try again.');
    } finally {
      this.clearing.set(false);
    }
  }

  private badgeClass(run: ScheduleRun): string {
    switch (run.status) {
      case 'notify':
        return `${BADGE} bg-state-info-100 text-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-300`;
      case 'completed':
        return `${BADGE} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
      case 'error':
      case 'timeout':
        return `${BADGE} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
      case 'paused':
        return `${BADGE} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
      case 'needs_user':
        return this.pausedBadgeClass(run);
      default:
        return `${BADGE} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  /** Waiting or waiting again reads as a warning; a finished resume as success, a failed one as danger. */
  private pausedBadgeClass(run: ScheduleRun): string {
    const warning = `${BADGE} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
    if (runStillWaiting(run.status, run) || run.resumeOutcome === 'waiting_again') return warning;
    if (run.resumeOutcome === 'finished') {
      return `${BADGE} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
    }
    if (run.resumeOutcome === 'failed') {
      return `${BADGE} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
    }
    return `${BADGE} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300`;
  }
}
