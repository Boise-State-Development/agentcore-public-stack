import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowUturnLeft, heroSparkles, heroXMark } from '@ng-icons/heroicons/outline';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../../components/confirmation-dialog/confirmation-dialog.component';
import { parseIso } from '../../utils/date';
import { MaintenanceFileResult, MaintenanceOp, MaintenanceRun, MemoryScope } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** How often a run in flight is re-read. A run takes minutes, so this is plenty. */
export const MAINTENANCE_POLL_MS = 4000;

/**
 * A finished tidy-up of your own memory is shown again when the page opens within this long,
 * so leaving mid-run and coming back still shows what changed and offers Undo. Older runs are
 * still undoable file by file, from History and the archive.
 */
export const RECENT_RUN_MS = 60 * 60 * 1000;

/** Runs whose summary was dismissed in this tab, so coming back to the page doesn't show it again. */
const dismissedRuns = new Set<string>();

interface StatusMessage {
  text: string;
  tone: 'info' | 'error';
  review?: boolean;
}

/**
 * "Tidy up" for project memory (shared-projects 2.6a, 2.6b): the button that starts a
 * maintenance run, and one live line that follows it until it ends.
 *
 * A run reads every file (or one) and merges items that repeat each other, replaces outdated
 * ones and removes ones whose dates have passed. A file near its size limit may also have a
 * sub-topic's items moved into a new file (2.6c).
 *
 * - **Project** memory: what it finds waits in the review queue, and nothing changes until an
 *   editor approves, which the confirmation says before anything starts.
 * - **Just me**: the changes are saved straight away. The line becomes a summary of what changed,
 *   file by file, with **Undo**, which puts back every file not saved since (the rest are named,
 *   and keep their History).
 *
 * A run already in flight when the page opens is picked up and followed, so leaving and coming
 * back shows where it got to.
 */
@Component({
  selector: 'app-memory-maintenance',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, RouterLink],
  providers: [provideIcons({ heroArrowUturnLeft, heroSparkles, heroXMark })],
  host: { class: 'contents' },
  template: `
    @if (canRun()) {
      <button
        type="button"
        (click)="start()"
        [disabled]="inFlight() || starting() || undoing()"
        class="inline-flex items-center gap-2 rounded-2xl border border-gray-200 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"
      >
        <ng-icon name="heroSparkles" class="size-4" aria-hidden="true" />
        {{ inFlight() ? 'Tidying up…' : 'Tidy up' }}
      </button>
    }
    <div role="status" aria-live="polite" class="basis-full empty:hidden">
      @if (message(); as m) {
        <div
          class="rounded-2xl border px-4 py-3 text-sm/6"
          [class]="m.tone === 'error'
            ? 'border-state-danger-200 bg-state-danger-50 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200'
            : 'border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200'"
        >
          <div class="flex items-start gap-3">
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
            @if (!inFlight() && !undoing()) {
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

          @if (applied().length && m.tone === 'info') {
            <details class="group mt-2">
              <summary class="w-fit cursor-pointer rounded-lg font-semibold text-primary-accessible underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50">
                What changed
              </summary>
              <ul class="mt-2 space-y-3" role="list">
                @for (file of applied(); track file.slug) {
                  <li>
                    <p class="flex flex-wrap items-baseline gap-x-2">
                      <a
                        [routerLink]="[]"
                        [queryParams]="{ file: file.slug, view: null }"
                        queryParamsHandling="merge"
                        class="font-semibold text-gray-900 underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-white"
                      >{{ file.slug }}</a>
                      @if (undoNote(file); as note) {
                        <span class="text-gray-600 dark:text-gray-300">{{ note }}</span>
                      }
                    </p>
                    @if (file.outcome === 'created') {
                      <p class="mt-1 text-gray-600 dark:text-gray-300">A new file, with the items moved from “{{ file.splitFrom }}”.</p>
                    } @else if (file.ops?.length) {
                      <ul class="mt-1 list-disc space-y-1 pl-5" role="list">
                        @for (op of file.ops; track $index) {
                          <li>{{ describe(op) }}</li>
                        }
                      </ul>
                    } @else {
                      <p class="mt-1 text-gray-600 dark:text-gray-300">{{ changes(file) }}. Too many to list here; the file’s History shows them.</p>
                    }
                  </li>
                }
              </ul>
            </details>
          }

          @if (undoableUntil(); as until) {
            <div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
              <button
                type="button"
                (click)="undo()"
                [disabled]="undoing()"
                class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1 text-sm/6 font-medium text-gray-800 transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 dark:hover:bg-gray-700"
              >
                <ng-icon name="heroArrowUturnLeft" class="size-4" aria-hidden="true" />
                {{ undoing() ? 'Undoing…' : 'Undo' }}
              </button>
              <span class="text-gray-600 dark:text-gray-300">You can undo this until {{ until | date: 'MMM d' }}.</span>
            </div>
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
  /** Which memory: the project's (owner and editors) or the member's own. */
  readonly scope = input<MemoryScope>('project');
  /** May start a run here: an owner or editor for project memory, any member for their own. */
  readonly canRun = input.required<boolean>();
  /** A run ended, or was undone: the page re-reads the pending count or the files. */
  readonly finished = output<MaintenanceRun>();

  protected readonly run = signal<MaintenanceRun | null>(null);
  protected readonly starting = signal(false);
  protected readonly startError = signal<string | null>(null);
  protected readonly undoing = signal(false);
  private readonly dismissed = signal(false);
  private timer: ReturnType<typeof setTimeout> | null = null;

  protected readonly inFlight = computed(() => {
    const state = this.run()?.state;
    return state === 'queued' || state === 'running';
  });

  /** Files the run saved changes to (your own memory), and the files its splits made. */
  protected readonly applied = computed(() =>
    this.dismissed() ? [] : (this.run()?.results ?? []).filter(r => r.outcome === 'applied' || r.outcome === 'created'),
  );

  protected readonly undoableUntil = computed(() => {
    const until = this.run()?.undoableUntil;
    return until && !this.dismissed() && this.run()?.state === 'done' ? parseIso(until) : null;
  });

  protected readonly message = computed<StatusMessage | null>(() => {
    const error = this.startError();
    if (error) return { text: error, tone: 'error' };
    const run = this.run();
    if (!run || this.dismissed()) return null;
    const mine = this.scope() === 'mine';
    const where = run.slug ? `“${run.slug}”` : mine ? 'your memory' : 'project memory';
    if (run.state === 'queued' || run.state === 'running') {
      return { text: `Tidying up ${where}. This takes a few minutes; you can leave this page.`, tone: 'info' };
    }
    if (run.state === 'failed') {
      return { text: run.error || 'Maintenance didn’t finish. Try again.', tone: 'error' };
    }
    if (run.undoneAt) return { text: undoneText(run), tone: 'info' };
    const count = (outcome: MaintenanceFileResult['outcome']) => run.results.filter(r => r.outcome === outcome).length;
    const failed = count('failed');
    const unreached = count('not_reached');
    const changed = run.results.filter(r => r.outcome === 'changed').map(r => `“${r.slug}”`);
    const extra = [
      changed.length ? `${list(changed)} ${changed.length === 1 ? 'was' : 'were'} saved while it ran, so ${changed.length === 1 ? 'it was' : 'they were'} left as ${changed.length === 1 ? 'it is' : 'they are'}.` : '',
      failed ? `${files(failed)} couldn’t be checked.` : '',
      unreached ? `${files(unreached)} weren’t reached; run it again for those.` : '',
    ].filter(Boolean).join(' ');
    const tail = extra ? ' ' + extra : '';
    if (mine) {
      const applied = run.results.filter(r => r.outcome === 'applied');
      if (!applied.length) return { text: `Maintenance found nothing to tidy in ${where}.${tail}`, tone: 'info' };
      const total = applied.reduce((n, r) => n + opCount(r), 0);
      const made = count('created');
      const moved = made ? `, moving items into ${plural(made, 'new file')}` : '';
      return { text: `Tidied up ${where}: ${plural(total, 'change')} to ${files(applied.length)}${moved}.${tail}`, tone: 'info' };
    }
    const proposed = count('proposed');
    if (!proposed) return { text: `Maintenance found nothing to tidy in ${where}.${tail}`, tone: 'info' };
    return {
      text: `Maintenance suggested changes to ${files(proposed)}. Nothing changes until an editor approves them.${tail}`,
      tone: 'info',
      review: true,
    };
  });

  constructor() {
    effect(() => {
      const id = this.projectId();
      const scope = this.scope();
      const allowed = this.canRun();
      untracked(() => {
        this.stop();
        this.run.set(null);
        this.startError.set(null);
        this.dismissed.set(false);
        if (allowed) void this.resume(id, scope);
      });
    });
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  /** Start a run over every file, or over `slug`, after the confirmation. */
  async start(slug?: string): Promise<void> {
    if (this.inFlight() || this.starting() || this.undoing()) return;
    const scope = this.scope();
    const target = slug ? 'this file' : 'every file';
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: scope === 'mine'
        ? {
            title: slug ? `Tidy up “${slug}”?` : 'Tidy up your memory?',
            message:
              `The assistant reads ${target} in your own memory here, merges items that repeat each other, ` +
              'replaces ones a newer item updates, and removes ones whose dates have passed. A file near its size limit ' +
              'may have a separate topic moved into a new file. The changes are saved ' +
              'straight away, and you can undo them afterwards. It takes a few minutes.',
            confirmText: 'Start',
          }
        : {
            title: slug ? `Tidy up “${slug}”?` : 'Tidy up project memory?',
            message:
              `The assistant reads ${target} and suggests merging items that repeat each other, ` +
              'replacing ones a newer item updates, removing ones whose dates have passed, and moving a large file’s ' +
              'separate topics into new files. Its suggestions wait in the ' +
              'review queue: nothing in memory changes until an editor approves them. It takes a few minutes.',
            confirmText: 'Start',
          },
    });
    if ((await firstValueFrom(ref.closed)) !== true) return;
    const projectId = this.projectId();
    this.starting.set(true);
    this.startError.set(null);
    try {
      const run = await firstValueFrom(this.api.startMaintenance(projectId, slug, scope));
      if (projectId !== this.projectId() || scope !== this.scope()) return;
      this.dismissed.set(false);
      this.run.set(run);
      if (run.state === 'queued' || run.state === 'running') this.schedule(projectId, scope);
      else this.finished.emit(run);
    } catch (err) {
      this.startError.set(projectErrorMessage(err, 'Maintenance couldn’t start. Try again in a minute.'));
    } finally {
      this.starting.set(false);
    }
  }

  /** Put back what the run changed in your own memory. */
  protected async undo(): Promise<void> {
    const current = this.run();
    if (!current || this.undoing()) return;
    const projectId = this.projectId();
    this.undoing.set(true);
    this.startError.set(null);
    try {
      const run = await firstValueFrom(this.api.undoMaintenance(projectId, current.runId));
      if (projectId !== this.projectId()) return;
      this.run.set(run);
      this.finished.emit(run);
    } catch (err) {
      this.startError.set(projectErrorMessage(err, 'The tidy-up couldn’t be undone. Try again in a minute.'));
    } finally {
      this.undoing.set(false);
    }
  }

  protected dismiss(): void {
    const run = this.run();
    if (run) dismissedRuns.add(run.runId);
    this.dismissed.set(true);
    this.startError.set(null);
  }

  protected describe(op: MaintenanceOp): string {
    const [first, second] = op.sources;
    if (op.type === 'merge') {
      return `Merged ${op.sources.length} items that said the same thing into “${op.text ?? ''}”`;
    }
    if (op.type === 'supersede') return `Removed “${first?.text ?? ''}”, since the newer “${second?.text ?? ''}” updates it`;
    if (op.type === 'split') return `Moved ${plural(op.sources.length, 'item')} into a new file, “${op.newSlug ?? ''}”`;
    return `Removed “${first?.text ?? ''}”, whose dates have passed`;
  }

  protected changes(file: MaintenanceFileResult): string {
    return plural(opCount(file), 'change');
  }

  /**
   * After an undo, what happened to one file. A split comes undone whole, so a file is left as
   * it is when the new file its items moved to was changed since, too.
   */
  protected undoNote(file: MaintenanceFileResult): string | null {
    switch (file.undo) {
      case 'restored':
        return 'Put back as it was.';
      case 'changed': {
        const made = (this.run()?.results ?? []).find(r => r.splitFrom === file.slug && r.undo !== 'removed');
        if (file.outcome === 'applied' && made && made.undo !== 'restored') {
          return `Left as it is, because “${made.slug}”, which its items moved to, changed since. Its History has the version from before.`;
        }
        return 'Saved after the tidy-up, so it was left as it is. Its History has the version from before.';
      }
      case 'missing':
        return 'Deleted since, so there was nothing to put back.';
      case 'failed':
        return 'Couldn’t be put back. Its History has the version from before.';
      case 'removed':
        return `Taken away again: its items are back in “${file.splitFrom ?? ''}”.`;
      default:
        return null;
    }
  }

  /** Follow a run that was in flight when the page opened, or show your latest tidy-up's result. */
  private async resume(projectId: string, scope: MemoryScope): Promise<void> {
    try {
      const { runs } = await firstValueFrom(this.api.maintenanceRuns(projectId, scope));
      const latest = runs[0];
      if (projectId !== this.projectId() || scope !== this.scope() || !latest) return;
      if (latest.state === 'queued' || latest.state === 'running') {
        this.run.set(latest);
        this.schedule(projectId, scope);
      } else if (scope === 'mine' && latest.undoableUntil && isRecent(latest) && !dismissedRuns.has(latest.runId)) {
        this.run.set(latest);
      }
    } catch {
      // Picking up a run is a courtesy; the button still works without it.
    }
  }

  private schedule(projectId: string, scope: MemoryScope): void {
    this.stop();
    this.timer = setTimeout(() => void this.poll(projectId, scope), MAINTENANCE_POLL_MS);
  }

  private async poll(projectId: string, scope: MemoryScope): Promise<void> {
    this.timer = null;
    const current = this.run();
    if (!current || projectId !== this.projectId() || scope !== this.scope()) return;
    try {
      const run = await firstValueFrom(this.api.maintenanceRun(projectId, current.runId, scope));
      if (projectId !== this.projectId() || scope !== this.scope()) return;
      this.run.set(run);
      if (run.state === 'queued' || run.state === 'running') {
        this.schedule(projectId, scope);
      } else {
        this.finished.emit(run);
      }
    } catch {
      // A blip shouldn't end the follow; try again on the next tick.
      this.schedule(projectId, scope);
    }
  }

  private stop(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

function undoneText(run: MaintenanceRun): string {
  // A file a split made goes with the file it came from, so only the files the run changed count.
  const undone = run.results.filter(r => r.undo && r.outcome === 'applied');
  const restored = undone.filter(r => r.undo === 'restored').length;
  const kept = undone.length - restored;
  if (!restored) return 'Nothing was undone: every file was changed or deleted after the tidy-up.';
  const back = `Undone: ${files(restored)} ${restored === 1 ? 'is' : 'are'} back as before.`;
  return kept ? `${back} ${files(kept)} changed since ${kept === 1 ? 'was' : 'were'} left as ${kept === 1 ? 'it is' : 'they are'}.` : back;
}

function isRecent(run: MaintenanceRun): boolean {
  const ended = run.finishedAt ? parseIso(run.finishedAt).getTime() : NaN;
  return Number.isFinite(ended) && Date.now() - ended < RECENT_RUN_MS;
}

function opCount(result: MaintenanceFileResult): number {
  return result.ops?.length ?? result.kept;
}

function list(names: string[]): string {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function plural(n: number, word: string): string {
  return `${n} ${n === 1 ? word : word + 's'}`;
}

function files(n: number): string {
  return plural(n, 'file');
}
