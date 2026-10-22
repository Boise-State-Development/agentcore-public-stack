import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ProjectMembersComponent } from './project-members.component';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';

/**
 * `/projects/:id/members` — who is in the project (`app-project-members`). A change
 * that moves the owner or the member count comes back as the project, which the
 * page keeps and hands on to the sidebar's list.
 */
@Component({
  selector: 'app-project-members-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectMembersComponent, ProjectSettingsShellComponent],
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading()"
      [error]="state.error()"
      heading="Members"
      [description]="description()"
    >
      @if (state.project(); as p) {
        <app-project-members [project]="p" (projectChange)="state.update($event)" />
      }
    </app-project-settings-shell>
  `,
})
export class ProjectMembersPage {
  /** Route param (`withComponentInputBinding`). */
  readonly id = input.required<string>();

  protected readonly state = injectProjectPage(this.id);
  protected readonly description = computed(() => {
    const p = this.state.project();
    const n = (p?.memberCount ?? 0) + 1;
    return n === 1 ? 'Just you so far. Everyone you add works with the same assistant.' : `${n} people work with the same assistant in this project.`;
  });
}
