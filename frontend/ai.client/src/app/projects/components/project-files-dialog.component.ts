import { ChangeDetectionStrategy, Component, Signal, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ProjectFilesComponent } from '../detail/project-files.component';
import { Project } from '../models/project.model';

export interface ProjectFilesDialogData {
  /** A signal so a rename or role change made elsewhere reaches the open dialog. */
  project: Signal<Project>;
  /** Called whenever the number of files changes, so the opener's count stays right. */
  onCountChange?: (count: number) => void;
}

/** Closes with nothing; the files list owns its own changes. */
export type ProjectFilesDialogResult = undefined;

/** The project's files, in a dialog (the Files list itself is `app-project-files`). */
@Component({
  selector: 'app-project-files-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ProjectFilesComponent],
  template: `
    <app-dialog-shell title="Files" [description]="description" size="lg" (closed)="close()">
      <app-project-files [project]="data.project()" (countChange)="data.onCountChange?.($event)" />
    </app-dialog-shell>
  `,
})
export class ProjectFilesDialogComponent {
  private dialogRef = inject<DialogRef<ProjectFilesDialogResult>>(DialogRef);
  protected readonly data = inject<ProjectFilesDialogData>(DIALOG_DATA);
  protected readonly description = `Documents the project’s assistant works from. Everyone in ${this.data.project().name} can open them.`;

  protected close(): void {
    this.dialogRef.close(undefined);
  }
}
