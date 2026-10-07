import { ChangeDetectionStrategy, Component, Signal, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ProjectActivityComponent } from '../detail/project-activity.component';
import { Project } from '../models/project.model';

export interface ProjectActivityDialogData {
  project: Signal<Project>;
}

export type ProjectActivityDialogResult = undefined;

/** The project's audit trail, in a dialog (the list itself is `app-project-activity`). */
@Component({
  selector: 'app-project-activity-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ProjectActivityComponent],
  template: `
    <app-dialog-shell
      title="Activity"
      description="Who changed what in this project. Only editors and the owner can see this."
      size="lg"
      (closed)="close()"
    >
      <app-project-activity [project]="data.project()" />
    </app-dialog-shell>
  `,
})
export class ProjectActivityDialogComponent {
  private dialogRef = inject<DialogRef<ProjectActivityDialogResult>>(DialogRef);
  protected readonly data = inject<ProjectActivityDialogData>(DIALOG_DATA);

  protected close(): void {
    this.dialogRef.close(undefined);
  }
}
