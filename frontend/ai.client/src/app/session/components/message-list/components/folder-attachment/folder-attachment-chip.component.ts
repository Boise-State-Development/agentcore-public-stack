import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroChevronDown, heroFolder, heroRectangleStack } from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import {
  SaveFolderAsProjectDialogComponent,
  SaveFolderAsProjectDialogData,
  SaveFolderAsProjectDialogResult,
} from '../../../../../projects/components/save-folder-as-project-dialog.component';
import { TooltipDirective } from '../../../../../components/tooltip';
import type { FolderAttachmentData } from '../../../../services/models/message.model';
import { FEATURES } from '../../../../../services/features';
import { FolderAttachmentError, FolderRefreshService } from '../../../../../services/folder-attachments';
import { ToastService } from '../../../../../services/toast/toast.service';

/**
 * A Drive folder on a sent user message (drive-folder spec §4.7): one chip,
 * name and file count, that expands to the files the model was given. On a
 * reloaded conversation the list comes from the message's own manifest, so it
 * is exactly what the model saw, bounded the same way.
 */
@Component({
  selector: 'app-folder-attachment-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroArrowPath, heroChevronDown, heroFolder, heroRectangleStack })],
  template: `
    <div
      class="w-72 max-w-full overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800"
      data-testid="folder-attachment-chip"
    >
      <div class="flex items-stretch">
      <button
        type="button"
        (click)="toggle()"
        [attr.aria-expanded]="expanded()"
        [attr.aria-controls]="listId"
        class="flex min-w-0 flex-1 items-center gap-3 px-3 py-2 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700/50"
      >
        <span class="flex size-9 shrink-0 items-center justify-center rounded-md border border-primary-200 bg-primary-50 text-primary-700 dark:border-primary-700 dark:bg-primary-900/40 dark:text-primary-300">
          <ng-icon name="heroFolder" class="size-5" aria-hidden="true" />
        </span>
        <span class="min-w-0 flex-1">
          <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ folder().name }}</span>
          <span class="block truncate text-xs text-gray-500 dark:text-gray-400">{{ subtitle() }}</span>
        </span>
        <ng-icon
          name="heroChevronDown"
          class="size-4 shrink-0 text-gray-400 transition-transform motion-reduce:transition-none"
          [class.rotate-180]="expanded()"
          aria-hidden="true"
        />
        <span class="sr-only">{{ expanded() ? 'Hide' : 'Show' }} the files in {{ folder().name }}</span>
      </button>
      @if (canRefresh) {
        <!-- Refresh (spec §4.6): re-list the folder and put it on the composer,
             so the next message carries the folder as it is now. -->
        <button
          type="button"
          (click)="refresh()"
          [disabled]="refreshing()"
          class="flex w-10 shrink-0 items-center justify-center border-l border-gray-200 text-gray-500 hover:bg-gray-50 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-wait disabled:opacity-60 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-700/50 dark:hover:text-gray-200"
          [attr.aria-label]="'Refresh ' + folder().name"
          data-testid="folder-chip-refresh"
        >
          <ng-icon name="heroArrowPath" class="size-4" [class.animate-spin]="refreshing()" aria-hidden="true" />
        </button>
      }
      @if (canSaveAsProject) {
        <!-- Save as project (composer-project-folder-picker spec §5): a
             project of its own with this folder's files, for a folder the
             person keeps coming back to. -->
        <button
          type="button"
          (click)="saveAsProject()"
          class="flex w-10 shrink-0 items-center justify-center border-l border-gray-200 text-gray-500 hover:bg-gray-50 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-700/50 dark:hover:text-gray-200"
          [attr.aria-label]="'Save ' + folder().name + ' as a project'"
          appTooltip="Save as project"
          data-testid="folder-chip-save-project"
        >
          <ng-icon name="heroRectangleStack" class="size-4" aria-hidden="true" />
        </button>
      }
      </div>
      @if (expanded()) {
        <ul [id]="listId" class="max-h-56 overflow-y-auto border-t border-gray-200 px-3 py-2 text-xs/5 text-gray-700 dark:border-gray-700 dark:text-gray-300">
          @for (path of folder().files; track $index) {
            <li class="truncate" [title]="path">{{ path }}</li>
          } @empty {
            <li class="text-gray-500 dark:text-gray-400">No readable files were attached yet.</li>
          }
          @if (folder().moreCount > 0) {
            <li class="text-gray-500 dark:text-gray-400">… and {{ folder().moreCount }} more</li>
          }
        </ul>
      }
    </div>
  `,
})
export class FolderAttachmentChipComponent {
  private static nextId = 0;
  private readonly folderRefresh = inject(FolderRefreshService);
  private readonly toast = inject(ToastService);
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);
  private readonly features = inject(FEATURES);

  readonly folder = input.required<FolderAttachmentData>();

  /** Refresh is part of the folder feature; without it the chip only lists files. */
  protected readonly canRefresh = this.features.folderAttachments;
  /** The folder's row comes from the folder routes, and the project from Projects: both must be on. */
  protected readonly canSaveAsProject = this.features.folderAttachments && this.features.projects;
  protected readonly refreshing = signal(false);

  protected readonly expanded = signal(false);
  protected readonly listId = `folder-files-${FolderAttachmentChipComponent.nextId++}`;

  protected readonly subtitle = computed(() => {
    const folder = this.folder();
    const count = `${folder.fileCount} ${folder.fileCount === 1 ? 'file' : 'files'}`;
    const lead = folder.status === 'expanding' ? `${count} so far` : count;
    return folder.source ? `${lead} · ${folder.source}` : lead;
  });

  protected toggle(): void {
    this.expanded.update((open) => !open);
  }

  protected async refresh(): Promise<void> {
    if (this.refreshing()) return;
    this.refreshing.set(true);
    try {
      await this.folderRefresh.refresh(this.folder());
      this.toast.info('Refreshing folder', `${this.folder().name} will go with your next message, as it is now.`);
    } catch (err) {
      const message =
        err instanceof FolderAttachmentError && err.status === 409
          ? "It's still being read, or Google Drive needs reconnecting. Try again in a moment."
          : err instanceof Error
            ? err.message
            : "Couldn't refresh the folder.";
      this.toast.error('Folder', message);
    } finally {
      this.refreshing.set(false);
    }
  }

  /**
   * Open "Save as project". On success the person stays in the conversation;
   * the toast opens the new project.
   */
  protected async saveAsProject(): Promise<void> {
    const ref = this.dialog.open<SaveFolderAsProjectDialogResult, SaveFolderAsProjectDialogData>(
      SaveFolderAsProjectDialogComponent,
      { data: { folder: this.folder() } },
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
