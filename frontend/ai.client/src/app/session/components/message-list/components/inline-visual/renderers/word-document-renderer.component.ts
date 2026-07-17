import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/**
 * Payload for the word_document inline visual, produced by the
 * create_word_document tool. download_url is a short-lived presigned S3 GET
 * URL whose response forces Content-Disposition: attachment, so a plain
 * click downloads the file (no new tab / navigation needed).
 */
interface WordDocumentPayload {
  filename: string;
  download_url: string;
  size_kb?: string;
}

/**
 * Inline download card for a generated Word document. Rendered as a
 * first-class message block (not inside the collapsed tool-output card), so
 * the download action is always visible and clickable.
 *
 * The download link uses the trailing-! important modifiers (text-white! and
 * no-underline!) because the global ".message-block a" rule in styles.css
 * (dark-blue text + underline) outranks a plain text-white utility by
 * specificity. The ! modifier emits !important, which wins regardless.
 */
@Component({
  selector: 'app-word-document-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (doc(); as d) {
      <div
        class="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700
               bg-white dark:bg-gray-800 p-3"
      >
        <!-- Document icon -->
        <div
          class="flex size-10 shrink-0 items-center justify-center rounded-md
                 bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400"
        >
          <svg class="size-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>
        </div>

        <!-- Filename + size -->
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
            {{ d.filename }}
          </p>
          @if (d.size_kb) {
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">{{ d.size_kb }}</p>
          }
        </div>

        <!-- Download button -->
        <a
          class="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary-500 px-3.5 py-1.5
                 text-sm/5 font-semibold text-white! no-underline! transition-colors
                 hover:bg-primary-700
                 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [href]="d.download_url"
          [attr.download]="d.filename"
          rel="noopener noreferrer"
        >
          <svg class="size-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
            />
          </svg>
          <span>Download</span>
        </a>
      </div>
    }
  `,
})
export class WordDocumentRendererComponent {
  /** The payload data from the backend tool result. */
  payload = input.required<unknown>();

  /** Narrowed, validated payload (null when malformed). */
  doc = computed<WordDocumentPayload | null>(() => {
    const raw = this.payload();
    if (!raw || typeof raw !== 'object') return null;
    const p = raw as Partial<WordDocumentPayload>;
    if (!p.filename || !p.download_url) return null;
    return { filename: p.filename, download_url: p.download_url, size_kb: p.size_kb };
  });
}
