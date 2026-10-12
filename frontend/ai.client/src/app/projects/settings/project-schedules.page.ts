import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroClock, heroPlus } from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { GrantService } from '../../schedules/services/grant.service';
import {
  cadenceSummary,
  relativeFuture,
  relativePast,
  runStatusLabel,
  scheduleStopSentence,
} from '../../schedules/schedule-wording';
import { parseIso } from '../../utils/date';
import { personLabel } from '../../shared/utils/person';
import { ProjectSchedule, ProjectScheduleRun } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { isUnavailable, projectErrorMessage } from '../services/projects.service';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';

interface RunsState {
  loading: boolean;
  runs: ProjectScheduleRun[];
  error: string | null;
}

/**
 * `/projects/:id/schedules` — a project's schedules (shared-projects 3.2c):
 * prompts that run on the project's assistant on a cadence, each as the member
 * who made it.
 *
 * Every member sees every schedule and its run history. What else someone can
 * do comes from the schedule itself (`canEdit`: its creator; `canManage`: its
 * creator, or an editor or the owner), never worked out here. Creating and
 * editing happen on the schedule form in its project mode, which returns here.
 */
@Component({
  selector: 'app-project-schedules-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, ProjectSettingsShellComponent, RouterLink],
  providers: [provideIcons({ heroChevronDown, heroClock, heroPlus })],
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading()"
      [error]="state.error()"
      heading="Schedules"
      description="Prompts that run on this project’s assistant on a cadence, each as the person who made it."
    >
      @if (canCreate() && available() && !loading()) {
        <button
          shellActions
          type="button"
          (click)="create()"
          class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
          New schedule
        </button>
      }

      @if (state.project()) {
        @if (error()) {
          <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
        }

        @if (loading()) {
          <div class="space-y-3" aria-busy="true">
            @for (i of [1, 2]; track i) {
              <div class="h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-700"></div>
            }
          </div>
        } @else if (!available()) {
          <div class="rounded-2xl border border-dashed border-gray-300 px-6 py-8 text-center dark:border-gray-700">
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">Scheduled runs aren’t turned on here.</p>
          </div>
        } @else {
          @if (needsGrant()) {
            <div class="mb-4 flex flex-col gap-3 rounded-2xl border border-state-info-200 bg-state-info-50 p-4 sm:flex-row sm:items-center sm:justify-between dark:border-state-info-900/40 dark:bg-state-info-900/10">
              <p class="text-sm/6 text-state-info-900 dark:text-state-info-200">
                Your schedules run as you while you’re away. Turn on scheduled runs so they can.
              </p>
              <button
                type="button"
                (click)="enableGrant()"
                [disabled]="grantBusy()"
                class="shrink-0 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {{ grantBusy() ? 'Turning on…' : 'Turn on scheduled runs' }}
              </button>
            </div>
          }

          @if (schedules().length === 0) {
            <div class="rounded-2xl border border-dashed border-gray-300 px-6 py-8 text-center dark:border-gray-700">
              <ng-icon name="heroClock" class="mx-auto size-6 text-gray-400 dark:text-gray-500" aria-hidden="true" />
              <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">No schedules yet.</p>
              <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                @if (canCreate()) {
                  A schedule runs a prompt here every day, every weekday, once a week or on an interval, and can share each run with the project.
                } @else {
                  Editors can add schedules that run a prompt here on a cadence.
                }
              </p>
            </div>
          } @else {
            <ul class="space-y-3">
              @for (s of schedules(); track s.scheduleId) {
                <li class="rounded-2xl border border-gray-200 p-4 dark:border-gray-700" [attr.data-schedule]="s.scheduleId">
                  <div class="flex flex-wrap items-center gap-2">
                    <h2 class="min-w-0 text-sm/6 font-medium break-words text-gray-900 dark:text-white">{{ s.label }}</h2>
                    <span [class]="badgeClass(s)">{{ stateLabel(s) }}</span>
                    @if (s.deliverTo === 'project') {
                      <span class="inline-flex items-center rounded-2xl bg-gray-100 px-2.5 py-0.5 text-xs/5 font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-200">Shared with the project</span>
                    }
                  </div>
                  <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">{{ cadence(s) }}</p>
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    Made by <span [attr.title]="!s.isMine && s.createdByName ? s.createdByEmail : null">{{ s.isMine ? 'you' : person(s) }}</span>
                    @if (s.deliverTo === 'project' && s.notifyMembers) {
                      · Members are told about each run
                    }
                    @if (s.deliverTo === 'project' && s.publishOutputs) {
                      · Publishes its artifacts to Outputs
                    }
                  </p>
                  @if (stopSentence(s); as stop) {
                    <p class="mt-1 text-xs/5 text-state-warning-700 dark:text-state-warning-400">{{ stop }}</p>
                  }
                  <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                    Next run: {{ nextRun(s) }} · Last run:
                    @if (s.lastRunSessionId && s.lastRunAt) {
                      <a
                        [routerLink]="['/s', s.lastRunSessionId]"
                        class="rounded-sm font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
                      >{{ lastRun(s) }}</a>
                    } @else {
                      {{ lastRun(s) }}
                    }
                  </p>
                  @if (actionError()[s.scheduleId]; as message) {
                    <p role="alert" class="mt-2 text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ message }}</p>
                  }

                  <div class="mt-3 flex flex-wrap items-center gap-1">
                    <button
                      type="button"
                      (click)="toggleRuns(s)"
                      [attr.aria-expanded]="isExpanded(s)"
                      [attr.aria-controls]="'runs-' + s.scheduleId"
                      class="-ml-2 inline-flex items-center gap-1 rounded-2xl px-2 py-1 text-xs/5 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      <ng-icon name="heroChevronDown" class="size-4 transition-transform" [class.rotate-180]="isExpanded(s)" aria-hidden="true" />
                      Run history
                    </button>
                    <span class="flex-1"></span>
                    @if (s.canEdit) {
                      <button type="button" (click)="edit(s)" [disabled]="isBusy(s)" [class]="actionClass">Edit</button>
                    }
                    @if (s.canManage && s.state === 'active') {
                      <button type="button" (click)="pause(s)" [disabled]="isBusy(s)" [class]="actionClass">Pause</button>
                    }
                    @if (s.canEdit && s.state !== 'active') {
                      <button type="button" (click)="resume(s)" [disabled]="isBusy(s)" [class]="actionClass">Resume</button>
                    }
                    @if (s.canManage) {
                      <button
                        type="button"
                        (click)="remove(s)"
                        [disabled]="isBusy(s)"
                        class="rounded-2xl px-3 py-1 text-xs/5 font-medium text-state-danger-600 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20"
                      >Delete</button>
                    }
                  </div>

                  @if (isExpanded(s)) {
                    <div [id]="'runs-' + s.scheduleId" class="mt-3 border-t border-gray-200 pt-3 dark:border-gray-700">
                      @if (runsOf(s); as state) {
                        @if (state.loading) {
                          <div class="h-12 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700" aria-busy="true"></div>
                        } @else if (state.error) {
                          <p role="alert" class="text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ state.error }}</p>
                        } @else if (state.runs.length === 0) {
                          <p class="text-xs/5 text-gray-600 dark:text-gray-400">It hasn’t run yet.</p>
                        } @else {
                          <ul class="space-y-1.5" [attr.aria-label]="'Runs of ' + s.label">
                            @for (run of state.runs; track run.runId) {
                              <li class="flex flex-wrap items-baseline gap-x-2 text-xs/5 text-gray-700 dark:text-gray-300">
                                <span class="size-1.5 shrink-0 translate-y-[-1px] self-center rounded-full" [class]="runDotClass(run)" aria-hidden="true"></span>
                                <span class="min-w-0 text-gray-600 dark:text-gray-400">
                                  <span class="font-medium text-gray-900 dark:text-white">{{ runLabel(run) }}</span> ·
                                  <time [attr.datetime]="run.finishedAt">{{ when(run) | date: 'MMM d, h:mm a' }}</time>
                                  @if (runNote(run, s); as note) {
                                    · {{ note }}
                                  }
                                </span>
                                @if (run.shareId) {
                                  <a
                                    [routerLink]="['/shared', run.shareId]"
                                    class="rounded-sm font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
                                  >Open the shared run</a>
                                }
                              </li>
                            }
                          </ul>
                        }
                      }
                    </div>
                  }
                </li>
              }
            </ul>
          }
        }
      }
    </app-project-settings-shell>
  `,
})
export class ProjectSchedulesPage {
  private api = inject(ProjectApiService);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private grants = inject(GrantService);

  /** Route param (`withComponentInputBinding`). */
  readonly id = input.required<string>();

  protected readonly state = injectProjectPage(this.id);

  protected readonly actionClass =
    'rounded-2xl px-3 py-1 text-xs/5 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-300 dark:hover:bg-gray-700';

  protected readonly schedules = signal<ProjectSchedule[]>([]);
  protected readonly loading = signal(true);
  protected readonly available = signal(true);
  protected readonly error = signal<string | null>(null);
  /** An action's refusal, shown on the schedule it was about (a resume's 409, say). */
  protected readonly actionError = signal<Readonly<Record<string, string>>>({});
  private readonly busy = signal<ReadonlySet<string>>(new Set());
  private readonly expanded = signal<ReadonlySet<string>>(new Set());
  private readonly runs = signal<Readonly<Record<string, RunsState>>>({});

  /** Editors and the owner create schedules, in an active project. */
  protected readonly canCreate = this.state.canEdit;

  protected readonly grantBusy = this.grants.loading$;
  private readonly grantChecked = signal(false);
  /** Someone whose schedules would run here, without the standing sign-in they run on. */
  protected readonly needsGrant = computed(
    () =>
      this.grantChecked() &&
      !this.grants.status$().enabled &&
      (this.canCreate() || this.schedules().some(s => s.isMine)),
  );

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
    });
  }

  private async load(projectId: string): Promise<void> {
    this.loading.set(true);
    this.available.set(true);
    this.error.set(null);
    try {
      const { schedules } = await firstValueFrom(this.api.schedules(projectId));
      if (projectId !== this.id()) return;
      this.schedules.set(schedules);
      void this.checkGrant();
    } catch (err) {
      if (projectId !== this.id()) return;
      if (isUnavailable(err)) {
        this.available.set(false);
      } else {
        this.error.set(projectErrorMessage(err, 'The schedules could not be loaded.'));
      }
    } finally {
      this.loading.set(false);
    }
  }

  private async checkGrant(): Promise<void> {
    if (!this.canCreate() && !this.schedules().some(s => s.isMine)) return;
    await this.grants.loadStatus();
    this.grantChecked.set(true);
  }

  protected async enableGrant(): Promise<void> {
    try {
      await this.grants.enable();
    } catch {
      this.error.set('Scheduled runs could not be turned on. Sign in again and try once more.');
    }
  }

  // ---- wording -------------------------------------------------------------

  protected cadence(s: ProjectSchedule): string {
    const summary = cadenceSummary(s);
    return s.cadence === 'interval' ? summary : `${summary} (${s.timezone})`;
  }

  protected person(s: ProjectSchedule): string {
    return personLabel(s.createdByName, s.createdByEmail);
  }

  protected stateLabel(s: ProjectSchedule): string {
    if (s.state === 'active') return 'Active';
    if (s.state === 'paused' || s.stateReason === 'paused_by_editor') return 'Paused';
    return 'Stopped';
  }

  protected badgeClass(s: ProjectSchedule): string {
    const base = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    if (s.state === 'active') {
      return `${base} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
    }
    if (this.stateLabel(s) === 'Stopped') {
      return `${base} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
    }
    return `${base} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200`;
  }

  /** Why it stopped, for a schedule that didn't stop because its creator paused it. */
  protected stopSentence(s: ProjectSchedule): string | null {
    return s.state === 'paused_error' ? scheduleStopSentence(s.stateReason, s.isMine) : null;
  }

  protected nextRun(s: ProjectSchedule): string {
    return s.state === 'active' && s.nextRunAt ? relativeFuture(s.nextRunAt) : '—';
  }

  protected lastRun(s: ProjectSchedule): string {
    if (!s.lastRunAt) return 'never';
    return `${runStatusLabel(s.lastRunStatus).toLowerCase()} ${relativePast(s.lastRunAt)}`;
  }

  protected runLabel(run: ProjectScheduleRun): string {
    if (run.status === 'completed' && run.shareId) return 'Completed and shared';
    return runStatusLabel(run.status);
  }

  /** A pause the dispatcher recorded says why; a failure's code is the creator's to read. */
  protected runNote(run: ProjectScheduleRun, s: ProjectSchedule): string | null {
    return run.status === 'paused' ? scheduleStopSentence(run.reason, s.isMine) : null;
  }

  protected runDotClass(run: ProjectScheduleRun): string {
    switch (run.status) {
      case 'completed':
        return 'bg-state-success-500';
      case 'paused':
        return 'bg-gray-400';
      default:
        return 'bg-state-warning-500';
    }
  }

  protected when(run: ProjectScheduleRun): Date {
    return parseIso(run.finishedAt);
  }

  // ---- run history -----------------------------------------------------------

  protected isExpanded(s: ProjectSchedule): boolean {
    return this.expanded().has(s.scheduleId);
  }

  protected runsOf(s: ProjectSchedule): RunsState | null {
    return this.runs()[s.scheduleId] ?? null;
  }

  protected toggleRuns(s: ProjectSchedule): void {
    const id = s.scheduleId;
    const open = !this.expanded().has(id);
    this.expanded.update(set => {
      const next = new Set(set);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
    if (open) void this.loadRuns(id);
  }

  /** Read again on every expand: a run can land while the page is open. */
  private async loadRuns(scheduleId: string): Promise<void> {
    const previous = this.runs()[scheduleId]?.runs ?? [];
    this.setRuns(scheduleId, { loading: previous.length === 0, runs: previous, error: null });
    try {
      const { runs } = await firstValueFrom(this.api.scheduleRuns(this.id(), scheduleId));
      this.setRuns(scheduleId, { loading: false, runs, error: null });
    } catch (err) {
      this.setRuns(scheduleId, { loading: false, runs: [], error: projectErrorMessage(err, 'The run history could not be loaded.') });
    }
  }

  private setRuns(scheduleId: string, state: RunsState): void {
    this.runs.update(all => ({ ...all, [scheduleId]: state }));
  }

  // ---- actions -----------------------------------------------------------------

  protected isBusy(s: ProjectSchedule): boolean {
    return this.busy().has(s.scheduleId);
  }

  protected create(): void {
    void this.router.navigate(['/schedules/new'], { queryParams: { projectId: this.id() } });
  }

  protected edit(s: ProjectSchedule): void {
    void this.router.navigate(['/schedules', s.scheduleId, 'edit'], { queryParams: { projectId: this.id() } });
  }

  protected pause(s: ProjectSchedule): Promise<void> {
    return this.act(s, 'That schedule could not be paused.', projectId => firstValueFrom(this.api.pauseSchedule(projectId, s.scheduleId)));
  }

  protected resume(s: ProjectSchedule): Promise<void> {
    return this.act(s, 'That schedule could not be resumed.', projectId => firstValueFrom(this.api.resumeSchedule(projectId, s.scheduleId)));
  }

  protected async remove(s: ProjectSchedule): Promise<void> {
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: {
        title: `Delete “${s.label}”?`,
        message: s.isMine
          ? 'It stops running for good. Runs it already shared stay shared. This can’t be undone.'
          : `It stops running for good, and ${this.person(s)} is told. Runs it already shared stay shared. This can’t be undone.`,
        confirmText: 'Delete schedule',
        destructive: true,
      },
    });
    if ((await firstValueFrom(ref.closed)) !== true) return;
    await this.act(s, 'That schedule could not be deleted.', async projectId => {
      await firstValueFrom(this.api.deleteSchedule(projectId, s.scheduleId));
      return null;
    });
  }

  /** Run one action on a schedule; an updated schedule replaces it, null removes it. */
  private async act(
    s: ProjectSchedule,
    fallback: string,
    call: (projectId: string) => Promise<ProjectSchedule | null>,
  ): Promise<void> {
    const id = s.scheduleId;
    this.busy.update(set => new Set(set).add(id));
    this.setActionError(id, null);
    try {
      const updated = await call(this.id());
      this.schedules.update(list =>
        updated ? list.map(x => (x.scheduleId === id ? updated : x)) : list.filter(x => x.scheduleId !== id),
      );
    } catch (err) {
      this.setActionError(id, projectErrorMessage(err, fallback));
    } finally {
      this.busy.update(set => {
        const next = new Set(set);
        next.delete(id);
        return next;
      });
    }
  }

  private setActionError(scheduleId: string, message: string | null): void {
    this.actionError.update(all => {
      const next = { ...all };
      if (message) next[scheduleId] = message;
      else delete next[scheduleId];
      return next;
    });
  }

}
