import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { ArtifactPreviewComponent } from '../../shared/artifact/artifact-preview.component';
import { artifactTypeStyle } from '../../shared/artifact/shared-artifact-card.component';
import { personLabel } from '../../shared/utils/person';
import { cadenceSummary } from '../../schedules/schedule-wording';
import { parseIso } from '../../utils/date';
import { Project, ProjectOutput, ProjectSchedule } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** The × on a card: shown on hover and keyboard focus, and always where there is no hover. */
const REMOVE_BUTTON =
  'absolute top-2 right-2 z-10 grid size-7 place-items-center rounded-full bg-white/90 text-gray-600 shadow-sm ring-1 ring-gray-900/5 backdrop-blur-sm transition-opacity hover:text-gray-900 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 disabled:opacity-50 dark:bg-gray-900/80 dark:text-gray-300 dark:ring-white/10 dark:hover:text-white opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100';

/**
 * A project's Outputs (shared-projects 3.3): artifacts members shared with
 * "Project members", on the project page between the composer and Recents.
 *
 * Each output is a sheet showing the top of the artifact (its stored excerpt,
 * drawn by `ArtifactPreviewComponent`); hovering lifts it and scrolls the page a
 * little, and an output on its second version or later fans out the sheets
 * beneath it. The newest output a schedule published (3.2d) leads the section
 * as a wider card with the schedule's cadence and next run, because a
 * recurring output is the one members come back for. The schedule's details
 * are read only when such an output exists, and the card still reads without
 * them (scheduled runs off, or the read failed).
 *
 * Each card opens the existing shared-artifact view (`/shared-artifact/{shareId}`),
 * which checks membership. The person who shared one, or an editor, can take it
 * off the project, which revokes that share. Renders nothing until something is
 * shared, so a project that never uses it sees no change.
 */
@Component({
  selector: 'app-project-outputs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ArtifactPreviewComponent, DatePipe, NgIcon, NgTemplateOutlet, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroXMark })],
  template: `
    @if (error()) {
      <p role="alert" class="mb-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    }
    @if (outputs().length > 0) {
      <section aria-labelledby="outputs-heading" class="@container mb-10">
        <h2 id="outputs-heading" class="text-sm/6 font-medium text-gray-600 dark:text-gray-400">Outputs</h2>

        @if (featured(); as f) {
          <div class="group relative mt-3" data-testid="featured-output">
            <a
              [routerLink]="['/shared-artifact', f.shareId]"
              [attr.aria-label]="label(f)"
              class="grid overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xs transition-shadow duration-300 group-hover:shadow-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 @md:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] dark:border-gray-700 dark:bg-gray-800 dark:group-hover:shadow-black/40"
            >
              <div class="relative h-44 overflow-hidden pt-4 pl-4 @md:h-auto @md:min-h-52" [class]="style(f).bg">
                <div class="absolute inset-0 top-4 left-4 overflow-hidden rounded-tl-xl bg-white shadow-sm ring-1 ring-gray-900/5 motion-safe:transition-transform motion-safe:duration-700 motion-safe:ease-out group-hover:-translate-x-1 group-hover:-translate-y-2.5 dark:bg-gray-900 dark:ring-white/10">
                  <app-artifact-preview [contentType]="f.contentType" [excerpt]="f.preview" />
                </div>
              </div>
              <div class="flex min-w-0 flex-col gap-2 p-5">
                <span class="flex items-center gap-2 text-xs/5 font-semibold tracking-wide text-primary-accessible uppercase dark:text-primary-50">
                  <span class="relative flex size-2" aria-hidden="true">
                    <span class="absolute inline-flex size-full rounded-full bg-primary-accessible opacity-60 motion-safe:animate-ping dark:bg-primary-400"></span>
                    <span class="relative inline-flex size-2 rounded-full bg-primary-accessible dark:bg-primary-400"></span>
                  </span>
                  Latest scheduled run
                </span>
                <span class="text-lg/7 font-semibold text-balance text-gray-900 dark:text-white">{{ f.title }}</span>
                <span class="grid gap-0.5 text-sm/6 text-gray-600 dark:text-gray-400">
                  @if (schedule(); as s) {
                    <span data-testid="schedule-cadence"><span class="font-medium text-gray-900 dark:text-gray-200">{{ cadence(s) }}</span> · published {{ sharedAt(f) | date: 'MMM d' }}</span>
                    @if (s.state === 'active' && s.nextRunAt) {
                      <span data-testid="schedule-next">Next run {{ nextRun(s) | date: 'EEE, MMM d' }}</span>
                    } @else {
                      <span data-testid="schedule-next">Paused</span>
                    }
                  } @else {
                    <span>Published {{ sharedAt(f) | date: 'MMM d' }}</span>
                  }
                  <span>{{ style(f).label }}@if (f.version > 1) { · v{{ f.version }} } · {{ sharer(f) }}</span>
                </span>
              </div>
            </a>
            @if (f.canRemove) {
              <ng-container *ngTemplateOutlet="removeButton; context: { $implicit: f }" />
            }
          </div>
        }

        @if (sheets().length > 0) {
          <ul class="mt-3 grid grid-cols-2 gap-4 @xl:grid-cols-3">
            @for (o of sheets(); track o.artifactId) {
              <li class="group relative">
                @if (o.version > 1) {
                  <div aria-hidden="true" class="absolute inset-0 translate-y-2 scale-[.92] rounded-2xl border border-gray-200 bg-white opacity-70 motion-safe:transition-transform motion-safe:duration-500 motion-safe:ease-out group-focus-within:translate-x-3 group-focus-within:translate-y-1.5 group-focus-within:rotate-[5deg] group-hover:translate-x-3 group-hover:translate-y-1.5 group-hover:rotate-[5deg] dark:border-gray-700 dark:bg-gray-800"></div>
                  <div aria-hidden="true" class="absolute inset-0 translate-y-1 scale-[.96] rounded-2xl border border-gray-200 bg-white motion-safe:transition-transform motion-safe:duration-500 motion-safe:ease-out group-focus-within:translate-x-1.5 group-focus-within:translate-y-0.5 group-focus-within:rotate-[2.5deg] group-hover:translate-x-1.5 group-hover:translate-y-0.5 group-hover:rotate-[2.5deg] dark:border-gray-700 dark:bg-gray-800"></div>
                }
                <a
                  [routerLink]="['/shared-artifact', o.shareId]"
                  [attr.aria-label]="label(o)"
                  class="relative block rounded-2xl border border-gray-200 bg-white shadow-xs focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 motion-safe:transition motion-safe:duration-300 motion-safe:ease-out group-focus-within:shadow-lg group-hover:shadow-lg motion-safe:group-focus-within:-translate-y-1 motion-safe:group-focus-within:-rotate-[.6deg] motion-safe:group-hover:-translate-y-1 motion-safe:group-hover:-rotate-[.6deg] dark:border-gray-700 dark:bg-gray-800 dark:group-hover:shadow-black/40"
                >
                  <div class="relative h-36 overflow-hidden rounded-t-2xl bg-white dark:bg-gray-800">
                    <div class="motion-safe:transition-transform motion-safe:duration-700 motion-safe:ease-out motion-safe:group-focus-within:-translate-y-6 motion-safe:group-hover:-translate-y-6">
                      <app-artifact-preview [contentType]="o.contentType" [excerpt]="o.preview" />
                    </div>
                    <div class="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-linear-to-b from-white/0 to-white dark:from-gray-800/0 dark:to-gray-800"></div>
                  </div>
                  <div class="grid gap-1 border-t border-gray-200 px-3.5 pt-2.5 pb-3 dark:border-gray-700">
                    <span class="line-clamp-2 text-sm/5 font-semibold text-gray-900 dark:text-white">{{ o.title }}</span>
                    <span class="flex flex-wrap items-center gap-1.5">
                      <span class="rounded-2xl px-2 text-xs/5 font-medium" [class]="style(o).bg + ' ' + style(o).text">{{ style(o).label }}</span>
                      @if (o.scheduleId) {
                        <span data-testid="scheduled-tag" class="rounded-2xl bg-gray-100 px-2 text-xs/5 font-medium text-gray-700 dark:bg-white/10 dark:text-gray-200">Scheduled</span>
                      }
                      @if (o.version > 1) {
                        <span class="text-xs/5 text-gray-600 tabular-nums dark:text-gray-400">v{{ o.version }}</span>
                      }
                    </span>
                    <span class="flex min-w-0 gap-1 text-xs/5 text-gray-600 dark:text-gray-400">
                      <span class="truncate">{{ sharer(o) }}</span><span class="shrink-0">· {{ sharedAt(o) | date: 'MMM d' }}</span>
                    </span>
                  </div>
                </a>
                @if (o.canRemove) {
                  <ng-container *ngTemplateOutlet="removeButton; context: { $implicit: o }" />
                }
              </li>
            }
          </ul>
        }
      </section>
    }

    <ng-template #removeButton let-o>
      <button
        type="button"
        (click)="remove(o)"
        [disabled]="busyId() !== null"
        [appTooltip]="o.isMine ? 'Stop sharing' : 'Remove from project'"
        [attr.aria-label]="(o.isMine ? 'Stop sharing ' : 'Remove from project ') + o.title"
        [class]="removeButtonClass"
      >
        <ng-icon name="heroXMark" size="1rem" aria-hidden="true" />
      </button>
    </ng-template>
  `,
})
export class ProjectOutputsComponent {
  private api = inject(ProjectApiService);
  private dialog = inject(Dialog);

  readonly project = input.required<Project>();

  protected readonly outputs = signal<ProjectOutput[]>([]);
  protected readonly error = signal<string | null>(null);
  protected readonly busyId = signal<string | null>(null);
  /** The featured output's schedule, once read; null while unread or unreadable. */
  protected readonly schedule = signal<ProjectSchedule | null>(null);
  protected readonly removeButtonClass = REMOVE_BUTTON;

  private readonly projectId = computed(() => this.project().projectId);

  /** The newest output a schedule published (the list is newest first). */
  protected readonly featured = computed(() => this.outputs().find(o => !!o.scheduleId) ?? null);
  protected readonly sheets = computed(() => {
    const featured = this.featured();
    return this.outputs().filter(o => o !== featured);
  });

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.outputs.set([]);
        this.schedule.set(null);
        void this.load(id);
      });
    });
  }

  protected label(o: ProjectOutput): string {
    return 'Open ' + this.style(o).label + ' ' + o.title + (o.scheduleId ? ', scheduled' : '') + ', ' + this.sharedByPhrase(o);
  }

  protected cadence(s: ProjectSchedule): string {
    return cadenceSummary(s);
  }

  protected nextRun(s: ProjectSchedule): Date {
    return parseIso(s.nextRunAt);
  }

  protected style(o: ProjectOutput) {
    return artifactTypeStyle(o.contentType || '');
  }

  protected sharer(o: ProjectOutput): string {
    return o.isMine ? 'Shared by you' : `Shared by ${personLabel(o.sharedByName, o.sharedByEmail)}`;
  }

  /** The same, mid-sentence, for the card's accessible name. */
  protected sharedByPhrase(o: ProjectOutput): string {
    return o.isMine ? 'shared by you' : `shared by ${personLabel(o.sharedByName, o.sharedByEmail)}`;
  }

  protected sharedAt(o: ProjectOutput): Date {
    return parseIso(o.sharedAt);
  }

  private async load(projectId: string): Promise<void> {
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.outputs(projectId));
      if (projectId !== this.projectId()) return;
      this.outputs.set(response.outputs);
      const scheduleId = this.featured()?.scheduleId;
      if (scheduleId) void this.loadSchedule(projectId, scheduleId);
    } catch (err) {
      if (projectId !== this.projectId()) return;
      this.error.set(projectErrorMessage(err, 'Couldn’t load the project’s outputs.'));
    }
  }

  /** The featured output's cadence and next run. Best-effort: the card reads without them. */
  private async loadSchedule(projectId: string, scheduleId: string): Promise<void> {
    try {
      const schedule = await firstValueFrom(this.api.schedule(projectId, scheduleId));
      if (projectId === this.projectId() && this.featured()?.scheduleId === scheduleId) this.schedule.set(schedule);
    } catch {
      // Scheduled runs off (404), the schedule deleted since, or a failed read.
    }
  }

  protected async remove(o: ProjectOutput): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: o.isMine ? 'Stop sharing this output?' : 'Remove this output from the project?',
        message: `“${o.title}” will no longer be listed in the project, and its link will stop working for members.`
          + (o.isMine ? '' : ' The artifact itself stays with the person who made it.'),
        confirmText: o.isMine ? 'Stop sharing' : 'Remove',
        destructive: true,
      } as ConfirmationDialogData,
    });
    if (!(await firstValueFrom(ref.closed))) return;
    const projectId = this.projectId();
    this.busyId.set(o.artifactId);
    this.error.set(null);
    try {
      await firstValueFrom(this.api.removeOutput(projectId, o.artifactId));
      this.outputs.update(list => list.filter(x => x.artifactId !== o.artifactId));
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That output couldn’t be removed. Try again.'));
    } finally {
      this.busyId.set(null);
    }
  }
}
