import { ChangeDetectionStrategy, Component, Signal, computed, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ProjectMembersComponent } from '../detail/project-members.component';
import { Project } from '../models/project.model';

export interface ProjectMembersDialogData {
  /** A signal so a transfer or rename reaches the open dialog. */
  project: Signal<Project>;
  /** The project after a change that moved its member count or owner. */
  onProjectChange: (project: Project) => void;
}

/** Closes with nothing; changes are reported through `onProjectChange`. */
export type ProjectMembersDialogResult = undefined;

/** Who is in the project, in a dialog (the list itself is `app-project-members`). */
@Component({
  selector: 'app-project-members-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ProjectMembersComponent],
  template: `
    <app-dialog-shell title="Members" [description]="description()" size="lg" (closed)="close()">
      <app-project-members [project]="data.project()" (projectChange)="data.onProjectChange($event)" />
    </app-dialog-shell>
  `,
})
export class ProjectMembersDialogComponent {
  private dialogRef = inject<DialogRef<ProjectMembersDialogResult>>(DialogRef);
  protected readonly data = inject<ProjectMembersDialogData>(DIALOG_DATA);
  protected readonly description = computed(() => {
    const n = this.data.project().memberCount + 1;
    return n === 1 ? 'Just you so far. Everyone you add works with the same assistant.' : `${n} people work with the same assistant in this project.`;
  });

  protected close(): void {
    this.dialogRef.close(undefined);
  }
}
