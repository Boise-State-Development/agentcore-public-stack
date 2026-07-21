import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

/**
 * Payload for the file_download inline visual, produced by the office document
 * tools (create_word_document, create_excel_spreadsheet, ...). download_url is
 * a short-lived presigned S3 GET URL whose response forces
 * Content-Disposition: attachment, so a plain click downloads the file (no new
 * tab / navigation needed).
 */
interface FileDownloadPayload {
  filename: string;
  download_url: string;
  size_kb?: string;
}

/** Per-file-kind icon + accent styling, chosen from the filename extension. */
interface FileKindStyle {
  /** SVG path drawn inside the icon badge. */
  iconPath: string;
  /** Tailwind classes for the icon badge (bg + text color, light + dark). */
  badgeClass: string;
}

const DOCUMENT_ICON =
  'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z';
const SPREADSHEET_ICON =
  'M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2';

const WORD_STYLE: FileKindStyle = {
  iconPath: DOCUMENT_ICON,
  badgeClass: 'bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
};
const EXCEL_STYLE: FileKindStyle = {
  iconPath: SPREADSHEET_ICON,
  badgeClass: 'bg-green-50 text-green-600 dark:bg-green-900/30 dark:text-green-400',
};
const GENERIC_STYLE: FileKindStyle = {
  iconPath: DOCUMENT_ICON,
  badgeClass: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

function styleForFilename(filename: string): FileKindStyle {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.xlsx') || lower.endsWith('.xls') || lower.endsWith('.csv')) {
    return EXCEL_STYLE;
  }
  if (lower.endsWith('.docx') || lower.endsWith('.doc')) {
    return WORD_STYLE;
  }
  return GENERIC_STYLE;
}

/**
 * Inline download card for a generated office file (Word .docx, Excel .xlsx,
 * ...). Rendered as a first-class message block (not inside the collapsed
 * tool-output card), so the download action is always visible and clickable.
 * The icon and accent color are chosen from the filename extension, so a single
 * `file_download` ui_type serves every office file type.
 *
 * The download link uses the trailing-! important modifiers (text-white! and
 * no-underline!) because the global ".message-block a" rule in styles.css
 * (dark-blue text + underline) outranks a plain text-white utility by
 * specificity. The ! modifier emits !important, which wins regardless.
 */
@Component({
  selector: 'app-file-download-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (file(); as f) {
      <div
        class="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700
               bg-white dark:bg-gray-800 p-3"
      >
        <!-- File-kind icon -->
        <div
          class="flex size-10 shrink-0 items-center justify-center rounded-md {{ f.badgeClass }}"
        >
          <svg class="size-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              [attr.d]="f.iconPath"
            />
          </svg>
        </div>

        <!-- Filename + size -->
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
            {{ f.filename }}
          </p>
          @if (f.size_kb) {
            <p class="text-xs/5 text-gray-500 dark:text-gray-400">{{ f.size_kb }}</p>
          }
        </div>

        <!-- Download button -->
        <a
          class="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-primary-500 px-3.5 py-1.5
                 text-sm/5 font-semibold text-white! no-underline! transition-colors
                 hover:bg-primary-700
                 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [href]="f.download_url"
          [attr.download]="f.filename"
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
export class FileDownloadRendererComponent {
  /** The payload data from the backend tool result. */
  payload = input.required<unknown>();

  /** Narrowed, validated payload with resolved icon styling (null when malformed). */
  file = computed<(FileDownloadPayload & FileKindStyle) | null>(() => {
    const raw = this.payload();
    if (!raw || typeof raw !== 'object') return null;
    const p = raw as Partial<FileDownloadPayload>;
    if (!p.filename || !p.download_url) return null;
    return {
      filename: p.filename,
      download_url: p.download_url,
      size_kb: p.size_kb,
      ...styleForFilename(p.filename),
    };
  });
}
