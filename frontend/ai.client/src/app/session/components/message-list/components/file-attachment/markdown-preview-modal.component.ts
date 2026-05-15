import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';
import { MarkdownComponent } from 'ngx-markdown';
import { FileUploadService } from '../../../../../services/file-upload';

/** Hard cap on how much of the file we render in the modal. */
const MAX_PREVIEW_BYTES = 1024 * 1024;

/**
 * Full-screen modal that fetches a markdown file via a short-lived presigned
 * URL and renders it through ngx-markdown. Used in place of opening the raw
 * source in a new tab when a user clicks a `.md` attachment card.
 */
@Component({
  selector: 'app-markdown-preview-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, MarkdownComponent],
  providers: [provideIcons({ heroXMark, heroArrowTopRightOnSquare })],
  host: {
    '(document:keydown)': 'onKeydown($event)',
  },
  template: `
    <div
      class="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      [attr.aria-label]="'Markdown preview: ' + filename()"
      (click)="onBackdropClick($event)"
    >
      <div
        class="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-900"
        (click)="$event.stopPropagation()"
      >
        <!-- Header -->
        <div
          class="flex items-center justify-between gap-3 border-b border-gray-200 px-5 py-3 dark:border-gray-700"
        >
          <h2
            class="min-w-0 truncate text-sm font-semibold text-gray-900 dark:text-white"
            [title]="filename()"
          >
            {{ filename() }}
          </h2>
          <div class="flex items-center gap-1">
            @if (sourceUrl()) {
              <a
                [href]="sourceUrl()!"
                target="_blank"
                rel="noopener noreferrer"
                class="flex size-9 items-center justify-center rounded-full text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                aria-label="Open raw markdown in a new tab"
              >
                <ng-icon
                  name="heroArrowTopRightOnSquare"
                  class="size-5"
                  aria-hidden="true"
                />
              </a>
            }
            <button
              type="button"
              class="flex size-9 items-center justify-center rounded-full text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              (click)="close.emit()"
              aria-label="Close preview"
            >
              <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
            </button>
          </div>
        </div>

        <!-- Body -->
        <div class="message-block min-h-0 flex-1 overflow-auto px-6 py-5">
          @switch (state()) {
            @case ('loading') {
              <div
                class="flex h-full min-h-[200px] items-center justify-center text-sm text-gray-500 dark:text-gray-400"
                role="status"
              >
                <span
                  class="size-5 animate-spin rounded-full border-2 border-gray-300 border-t-primary-500"
                  aria-hidden="true"
                ></span>
                <span class="ml-3">Loading preview…</span>
              </div>
            }
            @case ('error') {
              <div
                class="flex h-full min-h-[200px] items-center justify-center text-sm text-red-600 dark:text-red-400"
                role="alert"
              >
                Couldn't load the markdown preview.
              </div>
            }
            @case ('ready') {
              @if (truncated()) {
                <p
                  class="mb-4 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
                >
                  Showing the first {{ formattedLimit }} of this file. Open in a
                  new tab for the full source.
                </p>
              }
              <markdown [data]="content()" />
            }
          }
        </div>
      </div>
    </div>
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class MarkdownPreviewModalComponent {
  readonly uploadId = input.required<string>();
  readonly filename = input.required<string>();
  readonly close = output<void>();

  private readonly fileUploadService = inject(FileUploadService);

  protected readonly state = signal<'loading' | 'ready' | 'error'>('loading');
  protected readonly content = signal<string>('');
  protected readonly sourceUrl = signal<string | null>(null);
  protected readonly truncated = signal(false);

  protected readonly formattedLimit = computed(() => {
    const kb = MAX_PREVIEW_BYTES / 1024;
    return kb >= 1024 ? `${kb / 1024} MB` : `${kb} KB`;
  })();

  constructor() {
    effect(() => {
      const id = this.uploadId();
      if (id) this.load(id);
    });
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      this.close.emit();
    }
  }

  protected onBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) {
      this.close.emit();
    }
  }

  private async load(uploadId: string): Promise<void> {
    this.state.set('loading');
    try {
      const presigned = await this.fileUploadService.getPreviewUrl(uploadId);
      this.sourceUrl.set(presigned.url);

      const response = await fetch(presigned.url);
      if (!response.ok) {
        throw new Error(`Fetch failed: ${response.status}`);
      }

      const text = await response.text();
      const isTruncated = text.length > MAX_PREVIEW_BYTES;
      this.content.set(isTruncated ? text.slice(0, MAX_PREVIEW_BYTES) : text);
      this.truncated.set(isTruncated);
      this.state.set('ready');
    } catch {
      this.state.set('error');
    }
  }
}
