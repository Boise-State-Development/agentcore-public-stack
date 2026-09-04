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
  /** Epoch ms the presigned URL stops working; null when unknown. */
  expiresAt: number | null;
  /**
   * True while a re-minted URL is unproven. Set when a failed load triggers a
   * re-mint, cleared once an `<img>` actually loads — so a genuinely dead
   * object fails after one retry instead of looping.
   */
  reminted: boolean;
}

/** Treat a URL as expired this far ahead of its stated expiry. */
const EXPIRY_SKEW_MS = 30_000;

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
              (load)="onImageLoaded(item.attachment.uploadId)"
              (error)="onImageError(item.attachment.uploadId)"
            />
          } @else if (item.state.status === 'error') {
            <div
              class="flex size-full flex-col items-center justify-center gap-1 bg-state-danger-50 p-2 text-state-danger-500 dark:bg-state-danger-950/30 dark:text-state-danger-400"
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
        (imageError)="onLightboxImageError($event)"
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
      state:
        map.get(attachment.uploadId) ??
        ({ url: null, status: 'idle', expiresAt: null, reminted: false } as PreviewState),
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
    // The tile's own URL may have been minted well before this click. The
    // lightbox has no error tile of its own, so refresh a stale one up front
    // rather than showing a full-screen broken image.
    if (this.isStale(state)) {
      void this.mintPreview(attachment.uploadId, false);
    }
    this.lightboxOpenAt.set(visibleIndex);
  }

  /**
   * A tile failed to load. Presigned GET URLs expire after ~10 minutes, and
   * these tiles are `loading="lazy"` — so a message scrolled back into view
   * later in a long conversation fetches an already-dead signature. Re-mint
   * once; only call the preview unavailable if the fresh URL fails too.
   */
  protected onImageError(uploadId: string): void {
    const state = this.previews().get(uploadId);
    if (state?.reminted) {
      this.updatePreview(uploadId, {
        url: null,
        status: 'error',
        expiresAt: null,
        reminted: true,
      });
      return;
    }
    this.updatePreview(uploadId, {
      url: state?.url ?? null,
      status: 'loading',
      expiresAt: state?.expiresAt ?? null,
      reminted: true,
    });
    void this.mintPreview(uploadId, true);
  }

  /** A URL that rendered is proven good, so it earns a fresh retry budget. */
  protected onImageLoaded(uploadId: string): void {
    const state = this.previews().get(uploadId);
    if (!state?.reminted) return;
    this.updatePreview(uploadId, { ...state, reminted: false });
  }

  /** Same one-shot re-mint for a failure surfaced by the lightbox. */
  protected onLightboxImageError(index: number): void {
    const attachment = this.attachments()[index];
    if (!attachment) return;
    const state = this.previews().get(attachment.uploadId);
    if (state?.reminted) return;
    this.updatePreview(attachment.uploadId, {
      url: state?.url ?? null,
      status: state?.status ?? 'ready',
      expiresAt: state?.expiresAt ?? null,
      reminted: true,
    });
    void this.mintPreview(attachment.uploadId, true);
  }

  private isStale(state: PreviewState): boolean {
    if (state.expiresAt === null) return false;
    return Date.now() >= state.expiresAt - EXPIRY_SKEW_MS;
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
        next.set(a.uploadId, { url: null, status: 'loading', expiresAt: null, reminted: false });
      }
    }
    this.previews.set(next);

    await Promise.all(
      all.map(async (a) => {
        if (current.get(a.uploadId)?.status === 'ready') return;
        await this.mintPreview(a.uploadId, false);
      }),
    );
  }

  /**
   * Fetch a presigned GET URL and record when it dies.
   *
   * `reminted` carries through so the retry budget survives the round trip:
   * a first mint resets it, a re-mint keeps it spent until an `<img>` loads.
   */
  private async mintPreview(uploadId: string, reminted: boolean): Promise<void> {
    try {
      const response = await this.fileUploadService.getPreviewUrl(uploadId);
      const expiresAt = Date.parse(response.expiresAt);
      this.updatePreview(uploadId, {
        url: response.url,
        status: 'ready',
        expiresAt: Number.isNaN(expiresAt) ? null : expiresAt,
        reminted,
      });
    } catch {
      this.updatePreview(uploadId, {
        url: null,
        status: 'error',
        expiresAt: null,
        reminted,
      });
    }
  }

  private updatePreview(uploadId: string, state: PreviewState): void {
    this.previews.update((m) => {
      const next = new Map(m);
      next.set(uploadId, state);
      return next;
    });
  }
}
