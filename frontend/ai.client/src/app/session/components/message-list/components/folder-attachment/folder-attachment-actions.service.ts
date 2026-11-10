import { inject, Injectable } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import {
  SaveFolderAsProjectDialogComponent,
  SaveFolderAsProjectDialogData,
  SaveFolderAsProjectDialogResult,
} from '../../../../../projects/components/save-folder-as-project-dialog.component';
import { FEATURES } from '../../../../../services/features';
import { FolderAttachmentError, FolderRefreshService } from '../../../../../services/folder-attachments';
import { ToastService } from '../../../../../services/toast/toast.service';
import type { FolderAttachmentData } from '../../../../services/models/message.model';

/**
 * What a sent message's Drive folder offers (drive-folder spec §4.7): Refresh,
 * which brings its files up to date for the next message, and Save as project.
 * Shared by the folder chip and the folder attachment card, so both say the
 * same thing and gate on the same flags.
 */
@Injectable({ providedIn: 'root' })
export class FolderAttachmentActionsService {
  private readonly folderRefresh = inject(FolderRefreshService);
  private readonly toast = inject(ToastService);
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);
  private readonly features = inject(FEATURES);

  /** Refresh is part of the folder feature; without it a folder only lists its files. */
  readonly canRefresh = this.features.folderAttachments;
  /** The folder's row comes from the folder routes, and the project from Projects: both must be on. */
  readonly canSaveAsProject = this.features.folderAttachments && this.features.projects;

  /** Refresh the folder; reports the outcome as a toast and never throws. */
  async refresh(folder: FolderAttachmentData): Promise<void> {
    try {
      await this.folderRefresh.refresh(folder);
      this.toast.info('Refreshing folder', `${folder.name} will go with your next message, as it is now.`);
    } catch (err) {
      const message =
        err instanceof FolderAttachmentError && err.status === 409
          ? "It's still being read, or Google Drive needs reconnecting. Try again in a moment."
          : err instanceof Error
            ? err.message
            : "Couldn't refresh the folder.";
      this.toast.error('Folder', message);
    }
  }

  /**
   * Open "Save as project". On success the person stays in the conversation;
   * the toast opens the new project.
   */
  async saveAsProject(folder: FolderAttachmentData): Promise<void> {
    const ref = this.dialog.open<SaveFolderAsProjectDialogResult, SaveFolderAsProjectDialogData>(
      SaveFolderAsProjectDialogComponent,
      { data: { folder } },
    );
    const result = await firstValueFrom(ref.closed);
    if (!result) return;
    const projectId = result.project.projectId;
    const open = { label: 'Open project', handler: () => void this.router.navigate(['/projects', projectId]) };
    if (result.error) {
      void this.router.navigate(['/projects', projectId]);
      return;
    }
    this.toast.success(
      'Saved as project',
      `${result.project.name}: ${result.imported} ${result.imported === 1 ? 'file is' : 'files are'} being added to its Files.`,
      { action: open },
    );
  }
}
