import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ProjectHistoryComponent } from './project-history.component';

export interface ProjectHistoryDialogData {
  projectId: string;
}

export type ProjectHistoryDialogResult = undefined;

/** Every saved change to the project's settings, in a dialog (`app-project-history`). */
@Component({
  selector: 'app-project-history-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ProjectHistoryComponent],
  template: `
    <app-dialog-shell
      title="Settings history"
      description="Every saved change to the instructions, model, tools and skills, and who made it."
      size="lg"
      (closed)="close()"
    >
      <app-project-history [projectId]="data.projectId" />
    </app-dialog-shell>
  `,
})
export class ProjectHistoryDialogComponent {
  private dialogRef = inject<DialogRef<ProjectHistoryDialogResult>>(DialogRef);
  protected readonly data = inject<ProjectHistoryDialogData>(DIALOG_DATA);

  protected close(): void {
    this.dialogRef.close(undefined);
  }
}
