import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroTableCells,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { artifactTypeStyle } from '../../shared/artifact/shared-artifact-card.component';
import { personLabel } from '../../shared/utils/person';
import { parseIso } from '../../utils/date';
import { Project, ProjectOutput } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/**
 * A project's Outputs (shared-projects 3.3): artifacts members shared with
 * "Project members", on the project page between the composer and Recents.
 *
 * Each card opens the existing shared-artifact view (`/shared-artifact/{shareId}`),
 * which checks membership. The person who shared one, or an editor, can take it
 * off the project, which revokes that share. Renders nothing until something is
 * shared, so a project that never uses it sees no change.
 */
@Component({
  selector: 'app-project-outputs',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroCodeBracket, heroDocument, heroDocumentText, heroPhoto, heroTableCells, heroXMark })],
  template: `
    @if (error()) {
      <p role="alert" class="mb-6 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    }
    @if (outputs().length > 0) {
      <section aria-labelledby="outputs-heading" class="mb-10">
        <h2 id="outputs-heading" class="text-sm/6 font-medium text-gray-600 dark:text-gray-400">Outputs</h2>
        <ul class="mt-3 grid gap-3 sm:grid-cols-2">
          @for (o of outputs(); track o.artifactId) {
            <li class="relative">
              <a
                [routerLink]="['/shared-artifact', o.shareId]"
                [attr.aria-label]="'Open ' + style(o).label + ' ' + o.title + ', shared by ' + sharer(o)"
                class="flex w-full items-center gap-3 rounded-2xl border border-gray-200 bg-white px-3 py-2.5 pr-10 text-left transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700"
              >
                <span class="grid size-9 shrink-0 place-items-center rounded-2xl" [class]="style(o).bg" aria-hidden="true">
                  <ng-icon [name]="style(o).icon" class="size-5" [class]="style(o).text" />
                </span>
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-sm/6 font-semibold text-gray-900 dark:text-white">{{ o.title }}</span>
                  <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">
                    {{ style(o).label }}@if (o.version > 1) { · v{{ o.version }} } · {{ sharer(o) }} · {{ sharedAt(o) | date: 'MMM d' }}
                  </span>
                </span>
              </a>
              @if (o.canRemove) {
                <button
                  type="button"
                  (click)="remove(o)"
                  [disabled]="busyId() !== null"
                  [appTooltip]="o.isMine ? 'Stop sharing' : 'Remove from project'"
                  [attr.aria-label]="(o.isMine ? 'Stop sharing ' : 'Remove from project ') + o.title"
                  class="absolute top-1/2 right-2 grid size-7 -translate-y-1/2 place-items-center rounded-full text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
                >
                  <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
                </button>
              }
            </li>
          }
        </ul>
      </section>
    }
  `,
})
export class ProjectOutputsComponent {
  private api = inject(ProjectApiService);
  private dialog = inject(Dialog);

  readonly project = input.required<Project>();

  protected readonly outputs = signal<ProjectOutput[]>([]);
  protected readonly error = signal<string | null>(null);
  protected readonly busyId = signal<string | null>(null);

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.outputs.set([]);
        void this.load(id);
      });
    });
  }

  protected style(o: ProjectOutput) {
    return artifactTypeStyle(o.contentType || '');
  }

  protected sharer(o: ProjectOutput): string {
    return o.isMine ? 'Shared by you' : `Shared by ${personLabel(o.sharedByName, o.sharedByEmail)}`;
  }

  protected sharedAt(o: ProjectOutput): Date {
    return parseIso(o.sharedAt);
  }

  private async load(projectId: string): Promise<void> {
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.outputs(projectId));
      if (projectId === this.projectId()) this.outputs.set(response.outputs);
    } catch (err) {
      if (projectId !== this.projectId()) return;
      this.error.set(projectErrorMessage(err, 'Couldn’t load the project’s outputs.'));
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
