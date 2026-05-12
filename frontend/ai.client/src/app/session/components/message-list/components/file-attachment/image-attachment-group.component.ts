import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPhoto, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { FileAttachmentData } from '../../../../services/models/message.model';
import { FileUploadService } from '../../../../../services/file-upload';
import { ImageLightboxComponent, LightboxImage } from './image-lightbox.component';

interface PreviewState {
  url: string | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
}

/**
 * iMessage-style group renderer for one or more image attachments.
 *
 * Layouts:
 * - 1 image: large bubble (max 280px tall), aspect preserved
 * - 2 images: side-by-side equal columns
 * - 3 images: 1 large + 2 stacked column on the right
 * - 4 images: 2x2 grid
 * - 5+ images: 2x2 grid with "+N" overlay on the last tile
 *
 * Each image lazy-fetches a presigned GET URL on first render. Clicking any
 * tile opens a full-screen lightbox with arrow-key navigation.
 */
@Component({
  selector: 'app-image-attachment-group',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ImageLightboxComponent],
  providers: [provideIcons({ heroPhoto, heroExclamationTriangle })],
  host: { class: 'contents' },
  template: `
    <div
      class="overflow-hidden rounded-2xl"
      [class]="layoutClass()"
      [style.max-width.px]="maxWidthPx()"
    >
      @for (item of visibleImages(); track item.attachment.uploadId; let i = $index) {
        <button
          type="button"
          class="group relative block overflow-hidden bg-gray-100 dark:bg-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [class]="tileClass(i)"
          (click)="openLightbox(i)"
          [attr.aria-label]="'Open ' + item.attachment.filename"
        >
          @if (item.state.status === 'ready' && item.state.url) {
            <img
              [src]="item.state.url"
              [alt]="item.attachment.filename"
              class="size-full object-cover transition-transform duration-200 group-hover:scale-[1.02]"
              loading="lazy"
              decoding="async"
            />
          } @else if (item.state.status === 'error') {
            <div
              class="flex size-full flex-col items-center justify-center gap-1 bg-red-50 p-2 text-red-500 dark:bg-red-950/30 dark:text-red-400"
            >
              <ng-icon name="heroExclamationTriangle" class="size-6" aria-hidden="true" />
              <span class="px-2 text-center text-xs">Preview unavailable</span>
            </div>
          } @else {
            <div class="flex size-full items-center justify-center">
              <div
                class="size-8 animate-pulse rounded-full bg-gray-300 dark:bg-gray-600"
                aria-hidden="true"
              ></div>
              <span class="sr-only">Loading {{ item.attachment.filename }}</span>
            </div>
          }

          @if (showOverflowOnLast() && $last) {
            <div
              class="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/50 text-2xl font-semibold text-white"
            >
              +{{ overflowCount() }}
            </div>
          }
        </button>
      }
    </div>

    @if (lightboxOpenAt() !== null) {
      <app-image-lightbox
        [images]="lightboxImages()"
        [startIndex]="lightboxOpenAt() ?? 0"
        (close)="closeLightbox()"
      />
    }
  `,
})
export class ImageAttachmentGroupComponent {
  readonly attachments = input.required<FileAttachmentData[]>();

  private readonly fileUploadService = inject(FileUploadService);

  /** Map of uploadId -> preview state. Signals updates trigger re-render. */
  protected readonly previews = signal<Map<string, PreviewState>>(new Map());

  protected readonly lightboxOpenAt = signal<number | null>(null);

  /** All attachments are eligible for the lightbox; we cap visible tiles at 4. */
  private readonly maxVisible = 4;

  protected readonly visibleImages = computed(() => {
    const all = this.attachments();
    const visible = all.slice(0, this.maxVisible);
    const map = this.previews();
    return visible.map((attachment) => ({
      attachment,
      state: map.get(attachment.uploadId) ?? { url: null, status: 'idle' as const },
    }));
  });

  protected readonly overflowCount = computed(() =>
    Math.max(0, this.attachments().length - this.maxVisible),
  );

  protected readonly showOverflowOnLast = computed(() => this.overflowCount() > 0);

  protected readonly lightboxImages = computed<LightboxImage[]>(() => {
    const map = this.previews();
    return this.attachments().map((a) => ({
      url: map.get(a.uploadId)?.url ?? '',
      filename: a.filename,
    }));
  });

  protected readonly maxWidthPx = computed(() => {
    const count = Math.min(this.attachments().length, this.maxVisible);
    if (count === 1) return 320;
    return 360;
  });

  protected readonly layoutClass = computed(() => {
    const count = Math.min(this.attachments().length, this.maxVisible);
    if (count === 1) return 'block';
    if (count === 2) return 'grid grid-cols-2 gap-0.5';
    if (count === 3) return 'grid grid-cols-2 grid-rows-2 gap-0.5';
    return 'grid grid-cols-2 grid-rows-2 gap-0.5';
  });

  constructor() {
    queueMicrotask(() => this.loadPreviews());
  }

  protected tileClass(index: number): string {
    const count = Math.min(this.attachments().length, this.maxVisible);
    if (count === 1) {
      return 'aspect-[4/3] max-h-[280px] w-full';
    }
    if (count === 2) {
      return 'aspect-square';
    }
    if (count === 3) {
      // First tile spans 2 rows on left; tiles 2 and 3 stack on right
      if (index === 0) return 'row-span-2 aspect-[3/4]';
      return 'aspect-square';
    }
    // 4+
    return 'aspect-square';
  }

  protected openLightbox(visibleIndex: number): void {
    const map = this.previews();
    const attachment = this.attachments()[visibleIndex];
    if (!attachment) return;
    const state = map.get(attachment.uploadId);
    if (state?.status !== 'ready') return;
    this.lightboxOpenAt.set(visibleIndex);
  }

  protected closeLightbox(): void {
    this.lightboxOpenAt.set(null);
  }

  private async loadPreviews(): Promise<void> {
    const all = this.attachments();
    const current = this.previews();
    const next = new Map(current);
    for (const a of all) {
      if (!next.has(a.uploadId)) {
        next.set(a.uploadId, { url: null, status: 'loading' });
      }
    }
    this.previews.set(next);

    await Promise.all(
      all.map(async (a) => {
        if (current.get(a.uploadId)?.status === 'ready') return;
        try {
          const response = await this.fileUploadService.getPreviewUrl(a.uploadId);
          this.updatePreview(a.uploadId, { url: response.url, status: 'ready' });
        } catch {
          this.updatePreview(a.uploadId, { url: null, status: 'error' });
        }
      }),
    );
  }

  private updatePreview(uploadId: string, state: PreviewState): void {
    this.previews.update((m) => {
      const next = new Map(m);
      next.set(uploadId, state);
      return next;
    });
  }
}
