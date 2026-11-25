import { ChangeDetectionStrategy, Component, computed, inject, input, resource, signal } from '@angular/core';
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
import { cadenceSummary, lastDayRunSummary, relativePast, runStatusLabel } from '../schedule-wording';

/** "950", "12.3k", "1.2M". */
export function compactTokens(count: number): string {
  if (!Number.isFinite(count) || count < 1000) return String(Math.max(0, Math.round(count || 0)));
  if (count < 1_000_000) return `${trimZero((count / 1000).toFixed(1))}k`;
  return `${trimZero((count / 1_000_000).toFixed(1))}M`;
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
        label: runStatusLabel(run.status, run.itemCount),
        when: relativePast(run.finishedAt),
        absolute: parseIso(run.finishedAt).toLocaleString(),
        tokens: `${compactTokens(run.inputTokens)} in · ${compactTokens(run.outputTokens)} out · ${compactTokens(run.cacheReadTokens)} cached`,
        badgeClass: this.badgeClass(run.status),
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

  readonly skeletonRows = [0, 1, 2];

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

  private badgeClass(status: string): string {
    switch (status) {
      case 'notify':
        return `${BADGE} bg-state-info-100 text-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-300`;
      case 'completed':
        return `${BADGE} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
      case 'error':
      case 'timeout':
        return `${BADGE} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
      case 'paused':
        return `${BADGE} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
      default:
        return `${BADGE} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300`;
    }
  }
}
