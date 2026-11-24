import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowsPointingOut, heroPhoto } from '@ng-icons/heroicons/outline';

import {
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
} from '../../../../../../components/content-card/content-card.component';
import { FileUploadService } from '../../../../../../services/file-upload';
import {
  ImageLightboxComponent,
  type LightboxImage,
} from '../../file-attachment/image-lightbox.component';

/** Treat a URL as expired this far ahead of its stated expiry. */
const EXPIRY_SKEW_MS = 30_000;

type ImageState = 'loading' | 'ready' | 'failed';

/**
 * An image a tool made (a chart, a rendered slide sheet, a picture a script
 * wrote), shown whole in the conversation instead of as a file to download:
 * the image itself in the card's tray, scaled to fit and never cropped, then
 * the name, a `TYPE · size` line and Download. The image is a button that
 * opens it in the full-screen lightbox.
 *
 * The bytes come from a presigned URL that lives minutes, so the card mints
 * one when it is drawn and once more if the image fails to load (a card
 * scrolled back to later, or reopened in the lightbox). If that fails too, or
 * the browser cannot draw the format, the tray says so and Download, which
 * goes through the durable route, still works.
 */
@Component({
  selector: 'app-image-output-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, ImageLightboxComponent, NgIcon],
  providers: [provideIcons({ heroArrowsPointingOut, heroPhoto })],
  host: { class: 'block' },
  template: `
    <app-content-card
      size="lg"
      [bleed]="true"
      [fit]="true"
      [title]="filename()"
      [meta]="meta()"
      [status]="state() === 'failed' ? 'Preview unavailable' : ''"
    >
      <div contentCardPreview>
        @if (url(); as src) {
          <button
            type="button"
            class="group/image relative flex min-h-24 w-full cursor-zoom-in items-center justify-center focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-accessible"
            [attr.aria-label]="'Expand ' + filename()"
            (click)="expand()"
          >
            <img
              class="max-h-96 max-w-full object-contain"
              [src]="src"
              alt=""
              decoding="async"
              (load)="onLoad()"
              (error)="onError()"
            />
            <!-- Shown on hover and keyboard focus, and always where there is
                 no hover to reveal it (touch). -->
            <span
              class="absolute right-2 top-2 flex size-8 items-center justify-center rounded-full bg-gray-900/70 text-white opacity-0 transition-opacity group-hover/image:opacity-100 group-focus-visible/image:opacity-100 [@media(hover:none)]:opacity-100"
              aria-hidden="true"
            >
              <ng-icon name="heroArrowsPointingOut" size="16" />
            </span>
          </button>
        } @else if (state() === 'failed') {
          <div
            class="flex h-24 items-center justify-center text-gray-400 dark:text-gray-500"
            aria-hidden="true"
            data-testid="image-output-unavailable"
          >
            <ng-icon name="heroPhoto" size="28" />
          </div>
        } @else {
          <div
            class="h-48 bg-gray-200 motion-safe:animate-pulse dark:bg-gray-700"
            aria-hidden="true"
            data-testid="image-output-loading"
          ></div>
          <span class="sr-only" role="status">Loading {{ filename() }}</span>
        }
      </div>

      <a
        contentCardAction
        [class]="actionClass"
        [href]="href()"
        [attr.download]="filename()"
        [attr.aria-label]="'Download ' + filename()"
        rel="noopener noreferrer"
      >
        Download
      </a>
    </app-content-card>

    @if (lightboxOpen()) {
      <app-image-lightbox
        [images]="lightboxImages()"
        (imageError)="onError()"
        (close)="lightboxOpen.set(false)"
      />
    }
  `,
})
export class ImageOutputCardComponent {
  /** User-files upload id: the handle the preview URL is minted for. */
  readonly uploadId = input.required<string>();
  readonly filename = input.required<string>();
  /** The durable download route for this file. */
  readonly href = input.required<string>();
  /** The `TYPE · size` line. */
  readonly meta = input('');

  private readonly fileUploadService = inject(FileUploadService);

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;

  protected readonly state = signal<ImageState>('loading');
  protected readonly url = signal<string | null>(null);
  protected readonly lightboxOpen = signal(false);

  /** When the current URL dies, in epoch ms; null when the server did not say. */
  private expiresAt: number | null = null;
  /** The one retry has been spent; an image that loads earns it back. */
  private reminted = false;

  protected readonly lightboxImages = computed<LightboxImage[]>(() => [
    { url: this.url() ?? '', filename: this.filename() },
  ]);

  constructor() {
    effect(() => {
      const uploadId = this.uploadId();
      untracked(() => {
        this.reminted = false;
        this.state.set('loading');
        this.url.set(null);
        void this.mint(uploadId);
      });
    });
  }

  /** Open the lightbox, on a fresh URL if this one is about to expire. */
  protected expand(): void {
    if (this.expiresAt !== null && Date.now() >= this.expiresAt - EXPIRY_SKEW_MS) {
      void this.mint(this.uploadId());
    }
    this.lightboxOpen.set(true);
  }

  /** A URL that drew is proven good, so the next failure may retry again. */
  protected onLoad(): void {
    this.reminted = false;
    this.state.set('ready');
  }

  /**
   * The image did not load, in the card or in the lightbox: almost always an
   * expired signature. Mint once more; a second failure in a row means the
   * file is gone or the browser cannot draw it.
   */
  protected onError(): void {
    if (this.reminted) {
      this.fail();
      return;
    }
    this.reminted = true;
    void this.mint(this.uploadId());
  }

  private async mint(uploadId: string): Promise<void> {
    try {
      const response = await this.fileUploadService.getPreviewUrl(uploadId);
      if (uploadId !== this.uploadId()) return;
      // The retry was handed the URL that just failed: setting it again would
      // change nothing, so no second error would ever arrive to end the wait.
      if (this.reminted && response.url === this.url()) {
        this.fail();
        return;
      }
      const expiresAt = Date.parse(response.expiresAt);
      this.expiresAt = Number.isNaN(expiresAt) ? null : expiresAt;
      this.url.set(response.url);
    } catch {
      if (uploadId === this.uploadId()) this.fail();
    }
  }

  private fail(): void {
    this.url.set(null);
    this.expiresAt = null;
    this.lightboxOpen.set(false);
    this.state.set('failed');
  }
}
