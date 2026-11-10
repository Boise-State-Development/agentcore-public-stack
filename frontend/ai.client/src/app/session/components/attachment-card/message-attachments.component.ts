import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
} from '@angular/core';

import type { FileAttachmentData } from '../../services/models/message.model';
import { AttachmentCardComponent } from './attachment-card.component';
import {
  MESSAGE_MOSAIC_LIMIT,
  MESSAGE_MOSAIC_PREVIEW,
  mosaicLeadSpan,
} from './attachment-layout';

/**
 * A sent message's files, right-aligned under the bubble.
 *
 * One file is one card; two are a pair; three to five are a two-column mosaic
 * whose first card spans the gap the last row would leave, so rows are always
 * full. Six or more show the first three and a "+N more files" tile that
 * expands the same mosaic in place, so the layout never changes shape; "Show
 * fewer" folds it back. Below a container width of 28rem (a phone) the mosaic
 * is one column.
 */
@Component({
  selector: 'app-message-attachments',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AttachmentCardComponent],
  host: { class: 'block w-full @container' },
  template: `
    @if (attachments().length === 1) {
      <div class="flex justify-end">
        <app-attachment-card surface="message" [attachment]="attachments()[0]" />
      </div>
    } @else {
      <ul
        class="ml-auto grid max-w-[36rem] grid-cols-1 gap-2 @md:grid-cols-2"
        [attr.aria-label]="attachments().length + ' attached files'"
      >
        @for (attachment of visible(); track attachment.uploadId; let first = $first) {
          <li class="min-w-0" [class]="first ? leadSpanClass() : ''">
            <app-attachment-card class="h-full" surface="message" [fill]="true" [attachment]="attachment" />
          </li>
        }
        @if (hiddenCount() > 0) {
          <li class="min-w-0">
            <button
              type="button"
              class="flex size-full min-h-24 flex-col items-center justify-center gap-0.5 rounded-2xl bg-gray-100 text-gray-700 ring-1 ring-gray-200 transition-colors hover:bg-gray-200/70 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:bg-gray-800/70 dark:text-gray-200 dark:ring-white/10 dark:hover:bg-gray-800"
              [attr.aria-expanded]="false"
              [attr.aria-label]="'Show ' + hiddenCount() + ' more files'"
              (click)="expand()"
            >
              <span class="text-2xl/8 font-semibold tracking-tight">+{{ hiddenCount() }}</span>
              <span class="text-xs/5 text-gray-600 dark:text-gray-400">more files</span>
            </button>
          </li>
        }
      </ul>
      @if (expanded() && folds()) {
        <div class="mt-2 flex justify-end">
          <button
            type="button"
            class="rounded-lg px-3 py-1 text-sm/6 font-medium text-gray-700 ring-1 ring-gray-300 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-200 dark:ring-white/20 dark:hover:bg-white/10"
            [attr.aria-expanded]="true"
            (click)="expanded.set(false)"
          >
            Show fewer
          </button>
        </div>
      }
    }
  `,
})
export class MessageAttachmentsComponent {
  readonly attachments = input.required<FileAttachmentData[]>();

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  protected readonly expanded = signal(false);

  protected readonly folds = computed(() => this.attachments().length > MESSAGE_MOSAIC_LIMIT);

  protected readonly visible = computed(() =>
    this.folds() && !this.expanded()
      ? this.attachments().slice(0, MESSAGE_MOSAIC_PREVIEW)
      : this.attachments(),
  );

  protected readonly hiddenCount = computed(() => this.attachments().length - this.visible().length);

  /** The first card's span at two columns, counting the "+N" tile as a card. */
  protected readonly leadSpanClass = computed(() => {
    const slots = this.visible().length + (this.hiddenCount() > 0 ? 1 : 0);
    return mosaicLeadSpan(slots, 2) === 2 ? '@md:col-span-2' : '';
  });

  /** Expand in place, and move focus to the first card it revealed, since the
   *  tile that had focus is gone. */
  protected expand(): void {
    this.expanded.set(true);
    afterNextRender(
      () => {
        const items = this.host.nativeElement.querySelectorAll('li');
        items[MESSAGE_MOSAIC_PREVIEW]?.querySelector<HTMLElement>('button')?.focus();
      },
      { injector: this.injector },
    );
  }
}
