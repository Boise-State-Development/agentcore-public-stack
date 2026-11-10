import { ChangeDetectionStrategy, Component, computed } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroFolder, heroXMark } from '@ng-icons/heroicons/outline';

import { ContentCardComponent } from '../../../components/content-card/content-card.component';
import { FolderCardComponent } from '../chat-input/folder-card.component';
import { FolderPreviewComponent } from './folder-preview.component';

/**
 * A Drive folder on the composer, drawn as an attachment card
 * (`features.attachmentCards`). Same state and actions as `app-folder-card`,
 * which it extends: the walk's progress, Connect when the source's token
 * lapsed, Try again when a walk stopped short, and Remove.
 */
@Component({
  selector: 'app-composer-folder-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, FolderPreviewComponent, NgIcon],
  providers: [provideIcons({ heroArrowPath, heroFolder, heroXMark })],
  host: { class: 'block w-54 shrink-0' },
  template: `
    <app-content-card
      class="h-full"
      size="sm"
      data-testid="folder-card"
      [title]="folder().name"
      [label]="folder().name + ', folder, ' + statusText() + (skipReasons() ? '. Left out: ' + skipReasons() : '')"
      meta="Folder"
      [status]="statusText()"
      [live]="isBusy()"
      [tone]="isError() ? 'danger' : 'default'"
    >
      <app-folder-preview contentCardPreview [files]="fileNames()" [limit]="3" />
      <button
        contentCardCorner
        type="button"
        class="flex size-6 items-center justify-center rounded-full bg-white text-gray-600 shadow-sm ring-1 ring-gray-200 transition-colors hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:bg-gray-900 dark:text-gray-300 dark:ring-white/15 dark:hover:text-state-danger-400"
        [attr.aria-label]="'Remove folder ' + folder().name"
        (click)="onRemove($event)"
      >
        <ng-icon name="heroXMark" size="14" aria-hidden="true" />
      </button>
      @if (needsConsent()) {
        <button contentCardAction type="button" [class]="smallAction" (click)="onConnect($event)">
          Connect<span class="sr-only"> {{ folder().source }} to read {{ folder().name }}</span>
        </button>
      } @else if (canRetry()) {
        <button
          contentCardAction
          type="button"
          [class]="smallAction"
          [attr.aria-label]="'Try reading ' + folder().name + ' again'"
          (click)="onRetry($event)"
        >
          <ng-icon name="heroArrowPath" size="13" aria-hidden="true" />
          Retry
        </button>
      }
    </app-content-card>
  `,
})
export class ComposerFolderCardComponent extends FolderCardComponent {
  protected readonly smallAction =
    'inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-0.5 text-xs/5 font-medium text-gray-800 ring-1 ring-gray-300 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-100 dark:ring-white/20 dark:hover:bg-white/10';

  /** The files the walk has attached so far, for the preview. */
  protected readonly fileNames = computed(() =>
    (this.folder().row?.files ?? []).filter((file) => file.status === 'attached').map((file) => file.relativePath || file.name),
  );
}
