import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroArrowLeft, heroArrowRight } from '@ng-icons/heroicons/outline';

export interface LightboxImage {
  url: string;
  filename: string;
}

/**
 * Full-screen image lightbox with keyboard navigation.
 *
 * Renders a fixed overlay above the page when an image is selected.
 * Supports left/right arrow keys to step through a group of images and
 * Escape to dismiss.
 */
@Component({
  selector: 'app-image-lightbox',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark, heroArrowLeft, heroArrowRight })],
  host: {
    '(document:keydown)': 'onKeydown($event)',
  },
  template: `
    <div
      class="fixed inset-0 z-[9999] flex items-center justify-center bg-black/85 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      [attr.aria-label]="'Image preview: ' + currentImage().filename"
      (click)="onBackdropClick($event)"
    >
      <button
        type="button"
        class="absolute right-4 top-4 flex size-10 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        (click)="close.emit()"
        aria-label="Close preview"
      >
        <ng-icon name="heroXMark" class="size-6" aria-hidden="true" />
      </button>

      @if (hasMultiple()) {
        <button
          type="button"
          class="absolute left-4 top-1/2 flex size-12 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
          (click)="prev($event)"
          aria-label="Previous image"
        >
          <ng-icon name="heroArrowLeft" class="size-6" aria-hidden="true" />
        </button>
        <button
          type="button"
          class="absolute right-4 top-1/2 flex size-12 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
          (click)="next($event)"
          aria-label="Next image"
        >
          <ng-icon name="heroArrowRight" class="size-6" aria-hidden="true" />
        </button>
      }

      <figure class="flex max-h-full max-w-full flex-col items-center gap-3">
        <img
          [src]="currentImage().url"
          [alt]="currentImage().filename"
          class="max-h-[85vh] max-w-full rounded-lg object-contain shadow-2xl"
          (click)="$event.stopPropagation()"
        />
        <figcaption class="max-w-full truncate text-sm text-white/80">
          {{ currentImage().filename }}
          @if (hasMultiple()) {
            <span class="ml-2 text-white/50">{{ activeIndex() + 1 }} / {{ images().length }}</span>
          }
        </figcaption>
      </figure>
    </div>
  `,
})
export class ImageLightboxComponent {
  readonly images = input.required<LightboxImage[]>();
  readonly startIndex = input<number>(0);
  readonly close = output<void>();

  protected readonly activeIndex = signal(0);

  protected readonly currentImage = computed(() => {
    const list = this.images();
    const idx = Math.min(Math.max(this.activeIndex(), 0), list.length - 1);
    return list[idx];
  });

  protected readonly hasMultiple = computed(() => this.images().length > 1);

  constructor() {
    queueMicrotask(() => this.activeIndex.set(this.startIndex()));
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      this.close.emit();
    } else if (event.key === 'ArrowLeft' && this.hasMultiple()) {
      event.preventDefault();
      this.step(-1);
    } else if (event.key === 'ArrowRight' && this.hasMultiple()) {
      event.preventDefault();
      this.step(1);
    }
  }

  protected onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.close.emit();
    }
  }

  protected prev(event: Event): void {
    event.stopPropagation();
    this.step(-1);
  }

  protected next(event: Event): void {
    event.stopPropagation();
    this.step(1);
  }

  private step(delta: number): void {
    const len = this.images().length;
    if (len === 0) return;
    this.activeIndex.update((i) => (i + delta + len) % len);
  }
}
