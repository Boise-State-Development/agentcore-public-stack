import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { ProjectActivityComponent } from './project-activity.component';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';

/**
 * `/projects/:id/activity` — the project's audit trail (`app-project-activity`).
 * Editors and the owner only: a viewer who follows a link here is told so rather
 * than shown the server's refusal.
 */
@Component({
  selector: 'app-project-activity-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectActivityComponent, ProjectSettingsShellComponent],
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading()"
      [error]="state.error()"
      heading="Activity"
      description="Who changed what in this project. Only editors and the owner can see this."
    >
      @if (state.project(); as p) {
        @if (p.role === 'viewer') {
          <div class="rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-700">
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">Only editors and the owner can see this project’s activity.</p>
          </div>
        } @else {
          <app-project-activity [project]="p" />
        }
      }
    </app-project-settings-shell>
  `,
})
export class ProjectActivityPage {
  /** Route param (`withComponentInputBinding`). */
  readonly id = input.required<string>();

  protected readonly state = injectProjectPage(this.id);
}
