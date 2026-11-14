import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroFolder } from '@ng-icons/heroicons/outline';

import {
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
  type ContentCardMenuItem,
} from '../../../components/content-card/content-card.component';
import type { FolderAttachmentData } from '../../services/models/message.model';
import { FolderAttachmentActionsService } from '../message-list/components/folder-attachment/folder-attachment-actions.service';
import { folderSubtitle } from '../message-list/components/folder-attachment/folder-attachment-chip.component';
import { FolderPreviewComponent } from './folder-preview.component';

/**
 * A Drive folder on a sent message, drawn with the shared content card: its
 * first files in the tray, then its name and `N files · source`.
 *
 * "Files" opens the whole list the manifest holds below the card, and the
 * menu offers Refresh and Save as project, the folder chip's actions.
 */
@Component({
  selector: 'app-folder-attachment-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, FolderPreviewComponent, NgIcon],
  providers: [provideIcons({ heroFolder })],
  host: { class: 'block min-w-0', '[class]': "fill() ? 'w-full' : 'w-72 max-w-full'" },
  template: `
    <app-content-card
      class="h-full"
      [title]="folder().name"
      [label]="folder().name + ', folder, ' + subtitle()"
      [meta]="'Folder · ' + subtitle()"
      [status]="statusText()"
      [live]="folder().status === 'expanding' || refreshing()"
      [tone]="folder().status === 'failed' ? 'danger' : 'default'"
      [menuItems]="menuItems()"
      (menuSelect)="onMenu($event)"
    >
      <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1.5">
        <span class="flex shrink-0 text-primary-700 dark:text-primary-300" aria-hidden="true">
          <ng-icon name="heroFolder" size="13" />
        </span>
        <span
          class="min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-600 dark:bg-white/5 dark:text-gray-400"
          aria-hidden="true"
          >{{ folder().source || 'Folder' }}</span
        >
      </div>
      <app-folder-preview contentCardPreview [files]="folder().files" [moreCount]="folder().moreCount" />
      <button
        contentCardAction
        type="button"
        [class]="actionClass"
        [attr.aria-expanded]="expanded()"
        [attr.aria-controls]="listId"
        (click)="expanded.set(!expanded())"
      >
        Files<span class="sr-only"> in {{ folder().name }}</span>
      </button>
    </app-content-card>

    @if (expanded()) {
      <ul
        [id]="listId"
        class="mt-1 max-h-56 overflow-y-auto rounded-xl px-3 py-2 text-xs/5 text-gray-700 ring-1 ring-gray-200 dark:text-gray-300 dark:ring-white/10"
      >
        @for (path of folder().files; track $index) {
          <li class="truncate" [title]="path">{{ path }}</li>
        } @empty {
          <li class="text-gray-600 dark:text-gray-400">No readable files were attached yet.</li>
        }
        @if (folder().moreCount > 0) {
          <li class="text-gray-600 dark:text-gray-400">… and {{ folder().moreCount }} more</li>
        }
      </ul>
    }
  `,
})
export class FolderAttachmentCardComponent {
  private static nextId = 0;
  private readonly actions = inject(FolderAttachmentActionsService);

  readonly folder = input.required<FolderAttachmentData>();
  /** Fill the width the layout gives it, rather than the card's own. */
  readonly fill = input(false);

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;
  protected readonly expanded = signal(false);
  protected readonly refreshing = signal(false);
  protected readonly listId = `folder-card-files-${FolderAttachmentCardComponent.nextId++}`;

  protected readonly subtitle = computed(() => folderSubtitle(this.folder()));

  protected readonly statusText = computed(() => {
    if (this.refreshing()) return 'Refreshing…';
    const status = this.folder().status;
    if (status === 'partial') return 'Some files left out';
    if (status === 'failed') return "Couldn't read the folder";
    return '';
  });

  protected readonly menuItems = computed<ContentCardMenuItem[]>(() => [
    ...(this.actions.canRefresh ? [{ id: 'refresh', label: 'Refresh' }] : []),
    ...(this.actions.canSaveAsProject ? [{ id: 'save', label: 'Save as project' }] : []),
  ]);

  protected async onMenu(id: string): Promise<void> {
    if (id === 'save') {
      await this.actions.saveAsProject(this.folder());
      return;
    }
    if (id !== 'refresh' || this.refreshing()) return;
    this.refreshing.set(true);
    try {
      await this.actions.refresh(this.folder());
    } finally {
      this.refreshing.set(false);
    }
  }
}
