import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import {
  FolderAttachment,
  FolderAttachmentService,
  FolderRefreshService,
} from '../../services/folder-attachments';
import type { FolderAttachmentData } from '../../session/services/models/message.model';
import { projectErrorMessage } from '../services/projects.service';
import {
  SaveFolderAsProjectResult,
  SaveFolderAsProjectService,
  folderProjectDescription,
  importableFolderFiles,
} from '../services/save-folder-as-project.service';

export interface SaveFolderAsProjectDialogData {
  /** The sent chip's folder; its row is found in the conversation on screen. */
  folder: FolderAttachmentData;
}

/**
 * The saved project, or `undefined` when cancelled. A result with `error` set
 * means the project was created but some files were not added; the person saw
 * why and chose to open the project anyway.
 */
export type SaveFolderAsProjectDialogResult = SaveFolderAsProjectResult | undefined;

const NAME_MAX = 200;

/**
 * "Save as project" for a folder on a sent message
 * (composer-project-folder-picker spec §5, PR-3).
 */
@Component({
  selector: 'app-save-folder-as-project-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ReactiveFormsModule],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Save as project"
      description="A project you own, with this folder's files in its Files, so you can come back to them."
      (closed)="onCancel()"
    >
      <form [formGroup]="form" (ngSubmit)="onSave()" id="save-folder-project-form" class="space-y-4">
        <div>
          <label for="save-folder-project-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Project name</label>
          <input
            id="save-folder-project-name"
            type="text"
            cdkFocusInitial
            formControlName="name"
            [attr.maxlength]="nameMax"
            autocomplete="off"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <div aria-live="polite" class="text-sm/6 text-gray-700 dark:text-gray-300" data-testid="save-folder-summary">
          @if (loading()) {
            <span class="text-gray-600 dark:text-gray-400">Reading the folder…</span>
          } @else if (row()) {
            @if (fileCount() > 0) {
              {{ fileCount() }} {{ fileCount() === 1 ? 'file' : 'files' }} from {{ sourceName() }} will be added to the project's Files.
            } @else {
              This folder has no readable files to add yet.
            }
          }
        </div>

        <p class="rounded-xl bg-gray-50 px-3 py-2 text-xs/5 text-gray-600 dark:bg-white/5 dark:text-gray-400">
          The project keeps a copy of these files. Files added to the folder or changed in it later are not picked up, and folders inside it become one list.
        </p>

        @if (error(); as message) {
          <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400" data-testid="save-folder-error">{{ message }}</p>
        }
      </form>

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        @if (partial(); as result) {
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            Close
          </button>
          <button
            type="button"
            (click)="openPartial(result)"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
            data-testid="save-folder-open-partial"
          >
            Open project
          </button>
        } @else {
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            Cancel
          </button>
          <button
            type="submit"
            form="save-folder-project-form"
            [disabled]="!canSave()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="save-folder-submit"
          >
            {{ saving() ? 'Saving…' : 'Save as project' }}
          </button>
        }
      </div>
    </app-dialog-shell>
  `,
})
export class SaveFolderAsProjectDialogComponent implements OnInit {
  private readonly dialogRef = inject<DialogRef<SaveFolderAsProjectDialogResult>>(DialogRef);
  private readonly data = inject<SaveFolderAsProjectDialogData>(DIALOG_DATA);
  private readonly folderRefresh = inject(FolderRefreshService);
  private readonly folders = inject(FolderAttachmentService);
  private readonly saver = inject(SaveFolderAsProjectService);

  protected readonly nameMax = NAME_MAX;
  protected readonly loading = signal(true);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly row = signal<FolderAttachment | null>(null);
  /** The project exists but not every file went in: offer to open it rather than save again. */
  protected readonly partial = signal<SaveFolderAsProjectResult | null>(null);

  protected readonly form = new FormGroup({
    name: new FormControl(this.data.folder.name.slice(0, NAME_MAX), {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(NAME_MAX), Validators.pattern(/\S/)],
    }),
  });

  protected readonly fileCount = computed(() => {
    const row = this.row();
    return row ? importableFolderFiles(row).length : 0;
  });

  protected readonly sourceName = computed(
    () => this.row()?.connectorName || this.data.folder.source || 'the folder',
  );

  /** A name, a folder with files, and nothing in flight. */
  protected readonly nameValid = signal(this.form.valid);
  protected readonly canSave = computed(
    () => this.nameValid() && !this.loading() && !this.saving() && this.fileCount() > 0,
  );

  constructor() {
    this.form.statusChanges.pipe(takeUntilDestroyed()).subscribe(() => this.nameValid.set(this.form.valid));
  }

  async ngOnInit(): Promise<void> {
    try {
      const { sessionId, folderAttachId } = await this.folderRefresh.locate(this.data.folder);
      this.row.set(await this.folders.get(sessionId, folderAttachId));
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : "Couldn't read the folder.");
    } finally {
      this.loading.set(false);
    }
  }

  async onSave(): Promise<void> {
    const row = this.row();
    if (!row || !this.canSave()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const result = await this.saver.save(row, this.form.getRawValue().name.trim(), folderProjectDescription(row));
      if (result.error) {
        this.partial.set(result);
        this.error.set(
          `The project was created, but ${result.notImported} of ${result.imported + result.notImported} files were not added: ${result.error}`,
        );
        return;
      }
      this.dialogRef.close(result);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The project could not be created.'));
    } finally {
      this.saving.set(false);
    }
  }

  protected openPartial(result: SaveFolderAsProjectResult): void {
    this.dialogRef.close(result);
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
