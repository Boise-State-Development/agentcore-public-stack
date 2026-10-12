import { ChangeDetectionStrategy, Component, computed, input, numberAttribute } from '@angular/core';
import { ProjectHistoryComponent } from './project-history.component';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';

/**
 * `/projects/:id/history[?version=N]` — every saved change to the project's
 * settings (`app-project-history`). An Activity entry's Version link names the
 * version, which opens expanded.
 */
@Component({
  selector: 'app-project-history-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectHistoryComponent, ProjectSettingsShellComponent],
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading()"
      [error]="state.error()"
      heading="History"
      description="Every saved change to the instructions, model, tools and skills, and who made it."
    >
      @if (state.project(); as p) {
        <app-project-history [projectId]="p.projectId" [initialVersion]="openVersion()" />
      }
    </app-project-settings-shell>
  `,
})
export class ProjectHistoryPage {
  /** Route param and query param (`withComponentInputBinding`). */
  readonly id = input.required<string>();
  readonly version = input(undefined, { transform: (v: unknown) => (v == null ? undefined : numberAttribute(v, NaN)) });

  protected readonly state = injectProjectPage(this.id);
  protected readonly openVersion = computed(() => {
    const v = this.version();
    return v !== undefined && Number.isInteger(v) && v > 0 ? v : null;
  });
}
