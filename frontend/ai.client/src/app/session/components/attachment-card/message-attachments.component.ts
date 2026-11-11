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
import { NgTemplateOutlet } from '@angular/common';

import { FileUploadService } from '../../../services/file-upload';
import type { FileAttachmentData, FolderAttachmentData } from '../../services/models/message.model';
import {
  ImageLightboxComponent,
  type LightboxImage,
} from '../message-list/components/file-attachment/image-lightbox.component';
import { AttachmentCardComponent } from './attachment-card.component';
import {
  MESSAGE_MOSAIC_LIMIT,
  MESSAGE_MOSAIC_PREVIEW,
  mosaicLeadSpan,
} from './attachment-layout';
import { FolderAttachmentCardComponent } from './folder-attachment-card.component';

/** One card in the mosaic: a file (document or image) or a Drive folder. */
type MessageItem =
  | { key: string; file: FileAttachmentData; folder?: undefined }
  | { key: string; folder: FolderAttachmentData; file?: undefined };

/**
 * A sent message's attachments, right-aligned under the bubble: documents,
 * images and Drive folders, in one layout.
 *
 * One item is one card, up to 24rem wide; two are a pair; three to five are a two-column mosaic
 * whose first card spans the gap the last row would leave, so rows are always
 * full. Six or more show the first three and a "+N more" tile that expands the
 * same mosaic in place; "Show fewer" folds it back. Below a container width of
 * 28rem (a phone) the mosaic is one column.
 *
 * An image card opens the message's images in the lightbox, at that image,
 * with presigned URLs fetched when it opens (they live minutes).
 */
@Component({
  selector: 'app-message-attachments',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AttachmentCardComponent, FolderAttachmentCardComponent, ImageLightboxComponent, NgTemplateOutlet],
  host: { class: 'block w-full @container' },
  template: `
    @if (items().length === 1) {
      <!-- A lone card gets the browser card's width class (max-w-sm here), not
           a mosaic cell's: with Files or Preview and a menu beside it, 18rem
           left a folder's name cut to a dozen characters. -->
      <div class="flex justify-end">
        <div class="w-full max-w-sm">
          <ng-container [ngTemplateOutlet]="card" [ngTemplateOutletContext]="{ $implicit: items()[0], fill: true }" />
        </div>
      </div>
    } @else {
      <ul
        class="ml-auto grid max-w-[36rem] grid-cols-1 gap-2 @md:grid-cols-2"
        [attr.aria-label]="items().length + ' attachments'"
      >
        @for (item of visible(); track item.key; let first = $first) {
          <li class="min-w-0" [class]="first ? leadSpanClass() : ''">
            <ng-container [ngTemplateOutlet]="card" [ngTemplateOutletContext]="{ $implicit: item, fill: true }" />
          </li>
        }
        @if (hiddenCount() > 0) {
          <li class="min-w-0">
            <button
              type="button"
              class="flex size-full min-h-24 flex-col items-center justify-center gap-0.5 rounded-2xl bg-gray-100 text-gray-700 ring-1 ring-gray-200 transition-colors hover:bg-gray-200/70 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:bg-gray-800/70 dark:text-gray-200 dark:ring-white/10 dark:hover:bg-gray-800"
              [attr.aria-expanded]="false"
              [attr.aria-label]="'Show ' + hiddenCount() + ' more attachments'"
              (click)="expand()"
            >
              <span class="text-2xl/8 font-semibold tracking-tight">+{{ hiddenCount() }}</span>
              <span class="text-xs/5 text-gray-600 dark:text-gray-400">more</span>
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

    <ng-template #card let-item let-fill="fill">
      @if (item.file) {
        <app-attachment-card
          class="h-full"
          surface="message"
          [fill]="fill"
          [attachment]="item.file"
          (imageOpen)="openImage($event)"
        />
      } @else {
        <app-folder-attachment-card class="h-full" [fill]="fill" [folder]="item.folder" />
      }
    </ng-template>

    @if (lightbox(); as box) {
      <app-image-lightbox [images]="box.images" [startIndex]="box.start" (close)="lightbox.set(null)" />
    }
  `,
})
export class MessageAttachmentsComponent {
  readonly attachments = input<FileAttachmentData[]>([]);
  readonly folders = input<FolderAttachmentData[]>([]);

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private readonly fileUploadService = inject(FileUploadService);

  protected readonly expanded = signal(false);
  protected readonly lightbox = signal<{ images: LightboxImage[]; start: number } | null>(null);

  protected readonly items = computed<MessageItem[]>(() => [
    ...this.attachments().map((file) => ({ key: `file:${file.uploadId}`, file })),
    ...this.folders().map((folder, index) => ({ key: `folder:${folder.folderAttachId ?? index}`, folder })),
  ]);

  protected readonly folds = computed(() => this.items().length > MESSAGE_MOSAIC_LIMIT);

  protected readonly visible = computed(() =>
    this.folds() && !this.expanded() ? this.items().slice(0, MESSAGE_MOSAIC_PREVIEW) : this.items(),
  );

  protected readonly hiddenCount = computed(() => this.items().length - this.visible().length);

  /** The first card's span at two columns, counting the "+N" tile as a card. */
  protected readonly leadSpanClass = computed(() => {
    const slots = this.visible().length + (this.hiddenCount() > 0 ? 1 : 0);
    return mosaicLeadSpan(slots, 2) === 2 ? '@md:col-span-2' : '';
  });

  private readonly images = computed(() =>
    this.attachments().filter((file) => file.mimeType.startsWith('image/')),
  );

  /** Expand in place, and move focus to the first card it revealed, since the
   *  tile that had focus is gone. */
  protected expand(): void {
    this.expanded.set(true);
    afterNextRender(
      () => {
        const items = this.host.nativeElement.querySelectorAll('ul[aria-label] > li');
        items[MESSAGE_MOSAIC_PREVIEW]?.querySelector<HTMLElement>('button')?.focus();
      },
      { injector: this.injector },
    );
  }

  /** Open the message's images in the lightbox, at `uploadId`. An image whose
   *  URL cannot be fetched is left out rather than shown broken. */
  protected async openImage(uploadId: string): Promise<void> {
    const images = this.images();
    const fetched = await Promise.all(
      images.map(async (image) => {
        try {
          const { url } = await this.fileUploadService.getPreviewUrl(image.uploadId);
          return { uploadId: image.uploadId, url, filename: image.filename };
        } catch {
          return null;
        }
      }),
    );
    const shown = fetched.filter((image): image is NonNullable<typeof image> => image !== null);
    const start = shown.findIndex((image) => image.uploadId === uploadId);
    if (start < 0) return;
    this.lightbox.set({ images: shown.map(({ url, filename }) => ({ url, filename })), start });
  }
}
