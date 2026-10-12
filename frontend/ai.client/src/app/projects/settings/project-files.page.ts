import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { ProjectFilesComponent } from './project-files.component';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';

/** `/projects/:id/files` — the documents the project's assistant works from (`app-project-files`). */
@Component({
  selector: 'app-project-files-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectFilesComponent, ProjectSettingsShellComponent],
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading()"
      [error]="state.error()"
      heading="Files"
      description="Documents the project’s assistant works from."
    >
      @if (state.project(); as p) {
        <app-project-files [project]="p" />
      }
    </app-project-settings-shell>
  `,
})
export class ProjectFilesPage {
  /** Route param (`withComponentInputBinding`). */
  readonly id = input.required<string>();

  protected readonly state = injectProjectPage(this.id);
}
