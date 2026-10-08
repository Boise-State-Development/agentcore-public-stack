import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSparkles, heroXMark } from '@ng-icons/heroicons/outline';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../../components/confirmation-dialog/confirmation-dialog.component';
import { MaintenanceRun } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** How often a run in flight is re-read. A run takes minutes, so this is plenty. */
export const MAINTENANCE_POLL_MS = 4000;

/**
 * "Tidy up" for a project's shared memory (shared-projects 2.6a): the button that starts a
 * maintenance run, and one live line that follows it until it ends.
 *
 * A run reads every file (or one), suggests merging items that repeat each other,
 * replacing outdated ones and removing ones whose dates have passed, and leaves what it
 * finds in the review queue. Nothing changes until an editor approves, which the
 * confirmation says before anything starts.
 *
 * A run already in flight when the page opens is picked up and followed, so leaving
 * and coming back shows where it got to.
 */
@Component({
  selector: 'app-memory-maintenance',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroSparkles, heroXMark })],
  host: { class: 'contents' },
  template: `
    @if (canRun()) {
      <button
        type="button"
        (click)="start()"
        [disabled]="inFlight() || starting()"
        class="inline-flex items-center gap-2 rounded-2xl border border-gray-200 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
      >
        <ng-icon name="heroSparkles" class="size-4" aria-hidden="true" />
        {{ inFlight() ? 'Tidying up…' : 'Tidy up' }}
      </button>
    }
    <div role="status" aria-live="polite" class="basis-full empty:hidden">
      @if (message(); as m) {
        <div
          class="flex items-start gap-3 rounded-2xl border px-4 py-3 text-sm/6"
          [class]="m.tone === 'error'
            ? 'border-state-danger-200 bg-state-danger-50 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200'
            : 'border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200'"
        >
          <p class="min-w-0 flex-1">
            {{ m.text }}
            @if (m.review) {
              <a
                [routerLink]="[]"
                [queryParams]="{ view: 'review' }"
                queryParamsHandling="merge"
                class="ml-1 font-semibold text-primary-accessible underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
              >Review them</a>
            }
          </p>
          @if (!inFlight()) {
            <button
              type="button"
              (click)="dismiss()"
              aria-label="Dismiss"
              class="-m-1 grid size-7 shrink-0 place-items-center rounded-lg text-gray-500 hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
            >
              <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
            </button>
          }
        </div>
      }
    </div>
  `,
})
export class MemoryMaintenanceComponent {
  private api = inject(ProjectApiService);
  private dialog = inject(Dialog);

  readonly projectId = input.required<string>();
  /** Owner or editor of an active project. Anyone else sees nothing here. */
  readonly canRun = input.required<boolean>();
  /** A run ended: the page re-reads its pending count. */
  readonly finished = output<MaintenanceRun>();

  protected readonly run = signal<MaintenanceRun | null>(null);
  protected readonly starting = signal(false);
  protected readonly startError = signal<string | null>(null);
  private readonly dismissed = signal(false);
  private timer: ReturnType<typeof setTimeout> | null = null;

  protected readonly inFlight = computed(() => {
    const state = this.run()?.state;
    return state === 'queued' || state === 'running';
  });

  protected readonly message = computed<{ text: string; tone: 'info' | 'error'; review?: boolean } | null>(() => {
    const error = this.startError();
    if (error) return { text: error, tone: 'error' };
    const run = this.run();
    if (!run || this.dismissed()) return null;
    const scope = run.slug ? `“${run.slug}”` : 'project memory';
    if (run.state === 'queued' || run.state === 'running') {
      return { text: `Tidying up ${scope}. This takes a few minutes; you can leave this page.`, tone: 'info' };
    }
    if (run.state === 'failed') {
      return { text: run.error || 'Maintenance didn’t finish. Try again.', tone: 'error' };
    }
    const proposed = run.results.filter(r => r.outcome === 'proposed').length;
    const failed = run.results.filter(r => r.outcome === 'failed').length;
    const unreached = run.results.filter(r => r.outcome === 'not_reached').length;
    const extra = [
      failed ? `${files(failed)} couldn’t be checked.` : '',
      unreached ? `${files(unreached)} weren’t reached; run it again for those.` : '',
    ].filter(Boolean).join(' ');
    if (!proposed) {
      return { text: `Maintenance found nothing to tidy in ${scope}.${extra ? ' ' + extra : ''}`, tone: 'info' };
    }
    return {
      text: `Maintenance suggested changes to ${files(proposed)}. Nothing changes until an editor approves them.${extra ? ' ' + extra : ''}`,
      tone: 'info',
      review: true,
    };
  });

  constructor() {
    effect(() => {
      const id = this.projectId();
      const allowed = this.canRun();
      untracked(() => {
        this.stop();
        this.run.set(null);
        if (allowed) void this.resume(id);
      });
    });
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  /** Start a run over every file, or over `slug`, after the confirmation. */
  async start(slug?: string): Promise<void> {
    if (this.inFlight() || this.starting()) return;
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: {
        title: slug ? `Tidy up “${slug}”?` : 'Tidy up project memory?',
        message:
          'The assistant reads ' + (slug ? 'this file' : 'every file') + ' and suggests merging items that repeat each other, ' +
          'replacing ones a newer item updates, and removing ones whose dates have passed. Its suggestions wait in the ' +
          'review queue: nothing in memory changes until an editor approves them. It takes a few minutes.',
        confirmText: 'Start',
      },
    });
    if ((await firstValueFrom(ref.closed)) !== true) return;
    const projectId = this.projectId();
    this.starting.set(true);
    this.startError.set(null);
    try {
      const run = await firstValueFrom(this.api.startMaintenance(projectId, slug));
      if (projectId !== this.projectId()) return;
      this.dismissed.set(false);
      this.run.set(run);
      if (run.state === 'queued' || run.state === 'running') this.schedule(projectId);
      else this.finished.emit(run);
    } catch (err) {
      this.startError.set(projectErrorMessage(err, 'Maintenance couldn’t start. Try again in a minute.'));
    } finally {
      this.starting.set(false);
    }
  }

  protected dismiss(): void {
    this.dismissed.set(true);
    this.startError.set(null);
  }

  /** Follow a run that was already in flight when the page opened. */
  private async resume(projectId: string): Promise<void> {
    try {
      const { runs } = await firstValueFrom(this.api.maintenanceRuns(projectId));
      const latest = runs[0];
      if (projectId !== this.projectId() || !latest) return;
      if (latest.state === 'queued' || latest.state === 'running') {
        this.run.set(latest);
        this.schedule(projectId);
      }
    } catch {
      // Picking up a run in flight is a courtesy; the button still works without it.
    }
  }

  private schedule(projectId: string): void {
    this.stop();
    this.timer = setTimeout(() => void this.poll(projectId), MAINTENANCE_POLL_MS);
  }

  private async poll(projectId: string): Promise<void> {
    this.timer = null;
    const current = this.run();
    if (!current || projectId !== this.projectId()) return;
    try {
      const run = await firstValueFrom(this.api.maintenanceRun(projectId, current.runId));
      if (projectId !== this.projectId()) return;
      this.run.set(run);
      if (run.state === 'queued' || run.state === 'running') {
        this.schedule(projectId);
      } else {
        this.finished.emit(run);
      }
    } catch {
      // A blip shouldn't end the follow; try again on the next tick.
      this.schedule(projectId);
    }
  }

  private stop(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

function files(n: number): string {
  return `${n} ${n === 1 ? 'file' : 'files'}`;
}
