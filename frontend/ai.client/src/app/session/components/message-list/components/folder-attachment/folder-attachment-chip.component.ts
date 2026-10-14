import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroFolder } from '@ng-icons/heroicons/outline';
import type { FolderAttachmentData } from '../../../../services/models/message.model';

/**
 * A Drive folder on a sent user message (drive-folder spec §4.7): one chip,
 * name and file count, that expands to the files the model was given. On a
 * reloaded conversation the list comes from the message's own manifest, so it
 * is exactly what the model saw, bounded the same way.
 */
@Component({
  selector: 'app-folder-attachment-chip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroChevronDown, heroFolder })],
  template: `
    <div
      class="w-72 max-w-full overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800"
      data-testid="folder-attachment-chip"
    >
      <button
        type="button"
        (click)="toggle()"
        [attr.aria-expanded]="expanded()"
        [attr.aria-controls]="listId"
        class="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700/50"
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

  readonly folder = input.required<FolderAttachmentData>();

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
}
