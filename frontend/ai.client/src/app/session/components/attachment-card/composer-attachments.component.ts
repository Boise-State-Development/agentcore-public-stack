import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import type { FileMetadata, PendingUpload } from '../../../services/file-upload';
import { AttachmentCardComponent } from './attachment-card.component';
import { COMPOSER_RAIL_LIMIT } from './attachment-layout';

/** One card in the composer, from whichever source it came. */
type ComposerItem =
  | { id: string; restored: FileMetadata; upload?: undefined }
  | { id: string; upload: PendingUpload; restored?: undefined };

/**
 * The composer's attached files, above the input.
 *
 * Up to five sit in a single line that scrolls sideways, in the order they
 * were attached: restored with the draft first, then files arriving from a
 * connected source, then this tab's uploads. Past five the line folds into a
 * summary ("12 files · 9 ready · 2 uploading · 1 failed") with every file
 * that needs attention — failed, uploading or preparing — still drawn as a
 * card beside it, so a problem is never hidden in a collapsed stack. "Show
 * all" opens the full line.
 *
 * Anything projected (the Drive folder cards) goes at the end of the line.
 */
@Component({
  selector: 'app-composer-attachments',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AttachmentCardComponent],
  host: { class: 'block' },
  template: `
    @if (folds()) {
      <div class="flex items-center gap-3 px-4 pt-3">
        <div class="flex shrink-0" aria-hidden="true">
          @for (item of stackPreview(); track item.id; let i = $index) {
            <span
              class="-ml-2 h-8 w-6.5 rounded-md bg-white shadow-sm ring-1 ring-gray-200 first:ml-0 dark:bg-gray-800 dark:ring-white/10"
              [style.rotate.deg]="(i - 1) * 6"
            ></span>
          }
        </div>
        <p class="min-w-0 flex-1 truncate text-xs/5 text-gray-500 dark:text-gray-400" aria-live="polite">
          <span class="font-semibold text-gray-900 dark:text-gray-100">{{ items().length }} files</span>
          · {{ summary() }}
        </p>
        <button
          type="button"
          class="shrink-0 rounded-lg px-2.5 py-0.5 text-xs/5 font-medium text-gray-700 ring-1 ring-gray-300 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-200 dark:ring-white/20 dark:hover:bg-white/10"
          [attr.aria-expanded]="expanded()"
          (click)="expanded.set(!expanded())"
        >
          {{ expanded() ? 'Collapse' : 'Show all' }}
        </button>
      </div>
    }

    <div
      role="group"
      class="flex gap-2 overflow-x-auto px-4 py-3"
      style="mask-image: linear-gradient(to right, transparent, black 16px, black calc(100% - 24px), transparent); -webkit-mask-image: linear-gradient(to right, transparent, black 16px, black calc(100% - 24px), transparent);"
      [attr.aria-label]="items().length + ' attached files'"
    >
      @for (item of shown(); track item.id) {
        <div class="flex shrink-0">
          @if (item.upload) {
            <app-attachment-card
              surface="composer"
              [pendingUpload]="item.upload"
              (remove)="remove.emit($event)"
              (retry)="retry.emit($event)"
            />
          } @else {
            <app-attachment-card surface="composer" [file]="item.restored" (remove)="remove.emit($event)" />
          }
        </div>
      }
      <ng-content />
    </div>
  `,
})
export class ComposerAttachmentsComponent {
  /** Files that came back with the draft. */
  readonly restored = input<FileMetadata[]>([]);
  /** Files app-api is fetching from a connected source. */
  readonly importing = input<PendingUpload[]>([]);
  /** This tab's uploads. */
  readonly pending = input<PendingUpload[]>([]);

  readonly remove = output<string>();
  readonly retry = output<PendingUpload>();

  protected readonly expanded = signal(false);

  protected readonly items = computed<ComposerItem[]>(() => [
    ...this.restored().map((restored) => ({ id: restored.uploadId, restored })),
    ...this.importing().map((upload) => ({ id: upload.uploadId, upload })),
    ...this.pending().map((upload) => ({ id: upload.uploadId, upload })),
  ]);

  protected readonly folds = computed(() => this.items().length > COMPOSER_RAIL_LIMIT);

  private readonly needsAttention = computed(() =>
    this.items().filter((item) => item.upload !== undefined && item.upload.status !== 'ready'),
  );

  /** Folded, only the files that need attention stay drawn, failures first. */
  protected readonly shown = computed(() => {
    if (!this.folds() || this.expanded()) return this.items();
    return [...this.needsAttention()].sort(
      (a, b) => Number(b.upload?.status === 'error') - Number(a.upload?.status === 'error'),
    );
  });

  protected readonly stackPreview = computed(() => this.items().slice(0, 3));

  protected readonly summary = computed(() => {
    const count = { ready: 0, uploading: 0, preparing: 0, failed: 0 };
    for (const item of this.items()) {
      const status = item.upload?.status ?? (item.restored?.status === 'failed' ? 'error' : 'ready');
      if (status === 'uploading') count.uploading++;
      else if (status === 'completing') count.preparing++;
      else if (status === 'error') count.failed++;
      else count.ready++;
    }
    return (
      [
        [count.ready, 'ready'],
        [count.uploading, 'uploading'],
        [count.preparing, 'preparing'],
        [count.failed, 'failed'],
      ] as const
    )
      .filter(([n]) => n > 0)
      .map(([n, word]) => `${n} ${word}`)
      .join(' · ');
  });
}
