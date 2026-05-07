import { Component, ChangeDetectionStrategy, computed, effect, inject, input, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroDocument,
  heroDocumentText,
  heroTableCells,
  heroCodeBracket,
  heroPhoto,
  heroArrowTopRightOnSquare,
} from '@ng-icons/heroicons/outline';
import { formatBytes, FileUploadService } from '../../../../../services/file-upload';
import { FileAttachmentData } from '../../../../services/models/message.model';

interface FileTypeStyle {
  icon: string;
  label: string;
  /** Accent color used for the type chip and the icon */
  accent_text: string;
  /** Header strip background tint (subtle) */
  header_bg: string;
}

const DEFAULT_STYLE: FileTypeStyle = {
  icon: 'heroDocument',
  label: 'FILE',
  accent_text: 'text-gray-600 dark:text-gray-300',
  header_bg: 'bg-gray-50 dark:bg-gray-700/50',
};

const FILE_TYPE_STYLES: Record<string, FileTypeStyle> = {
  'application/pdf': {
    icon: 'heroDocument',
    label: 'PDF',
    accent_text: 'text-rose-600 dark:text-rose-300',
    header_bg: 'bg-rose-50 dark:bg-rose-950/40',
  },
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {
    icon: 'heroDocumentText',
    label: 'DOCX',
    accent_text: 'text-blue-600 dark:text-blue-300',
    header_bg: 'bg-blue-50 dark:bg-blue-950/40',
  },
  'text/plain': {
    icon: 'heroDocumentText',
    label: 'TXT',
    accent_text: 'text-gray-600 dark:text-gray-300',
    header_bg: 'bg-gray-50 dark:bg-gray-700/50',
  },
  'text/html': {
    icon: 'heroCodeBracket',
    label: 'HTML',
    accent_text: 'text-orange-600 dark:text-orange-300',
    header_bg: 'bg-orange-50 dark:bg-orange-950/40',
  },
  'text/csv': {
    icon: 'heroTableCells',
    label: 'CSV',
    accent_text: 'text-green-600 dark:text-green-300',
    header_bg: 'bg-green-50 dark:bg-green-950/40',
  },
  'application/vnd.ms-excel': {
    icon: 'heroTableCells',
    label: 'XLS',
    accent_text: 'text-green-600 dark:text-green-300',
    header_bg: 'bg-green-50 dark:bg-green-950/40',
  },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': {
    icon: 'heroTableCells',
    label: 'XLSX',
    accent_text: 'text-green-600 dark:text-green-300',
    header_bg: 'bg-green-50 dark:bg-green-950/40',
  },
  'text/markdown': {
    icon: 'heroDocumentText',
    label: 'MD',
    accent_text: 'text-purple-600 dark:text-purple-300',
    header_bg: 'bg-purple-50 dark:bg-purple-950/40',
  },
  'image/png': {
    icon: 'heroPhoto',
    label: 'PNG',
    accent_text: 'text-indigo-600 dark:text-indigo-300',
    header_bg: 'bg-indigo-50 dark:bg-indigo-950/40',
  },
  'image/jpeg': {
    icon: 'heroPhoto',
    label: 'JPG',
    accent_text: 'text-indigo-600 dark:text-indigo-300',
    header_bg: 'bg-indigo-50 dark:bg-indigo-950/40',
  },
  'image/gif': {
    icon: 'heroPhoto',
    label: 'GIF',
    accent_text: 'text-indigo-600 dark:text-indigo-300',
    header_bg: 'bg-indigo-50 dark:bg-indigo-950/40',
  },
  'image/webp': {
    icon: 'heroPhoto',
    label: 'WEBP',
    accent_text: 'text-indigo-600 dark:text-indigo-300',
    header_bg: 'bg-indigo-50 dark:bg-indigo-950/40',
  },
};

const TEXT_PREVIEW_MIMES = new Set(['text/plain', 'text/markdown', 'text/csv', 'text/html']);

/** Skeleton "lines of text" widths (percent), tuned to look like a paragraph. */
const SKELETON_LINE_WIDTHS = [92, 78, 88, 64, 95, 70, 84, 58];

/**
 * Document-style preview card for a non-image file attachment.
 *
 * Renders an iMessage-inspired "paper" mockup: a tinted header strip with the
 * type chip and accent icon, a white page area showing either a real text
 * excerpt (for txt/md/csv/html) or skeleton lines (for binary docs), a
 * folded top-right corner detail, and a footer with filename + size.
 *
 * Clicking opens the file in a new tab via a short-lived presigned URL.
 */
@Component({
  selector: 'app-file-attachment-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroDocument,
      heroDocumentText,
      heroTableCells,
      heroCodeBracket,
      heroPhoto,
      heroArrowTopRightOnSquare,
    }),
  ],
  host: { class: 'contents' },
  styles: `
    .corner-fold {
      width: 18px;
      height: 18px;
      background: linear-gradient(225deg, var(--corner-bg, #f3f4f6) 50%, transparent 50%);
      box-shadow: -1px 1px 1px rgb(0 0 0 / 0.05);
    }
    :host-context(.dark) .corner-fold {
      --corner-bg: #374151;
    }
  `,
  template: `
    <button
      type="button"
      (click)="openFile()"
      class="group flex w-60 shrink-0 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white text-left shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800"
      [attr.aria-label]="'Open ' + attachment().filename"
    >
      <!-- Header strip -->
      <div
        class="flex items-center justify-between border-b border-gray-200 px-3 py-2 dark:border-gray-700"
        [class]="style().header_bg"
      >
        <div class="flex items-center gap-2">
          <ng-icon
            [name]="style().icon"
            class="size-4"
            [class]="style().accent_text"
            aria-hidden="true"
          />
          <span
            class="text-[10px] font-bold tracking-wider"
            [class]="style().accent_text"
          >
            {{ style().label }}
          </span>
        </div>
        <ng-icon
          name="heroArrowTopRightOnSquare"
          class="size-4 text-gray-400 opacity-0 transition-opacity group-hover:opacity-100"
          aria-hidden="true"
        />
      </div>

      <!-- Paper page area -->
      <div class="relative h-32 overflow-hidden bg-white dark:bg-gray-900/40">
        <!-- Folded corner -->
        <div
          class="corner-fold absolute right-0 top-0"
          aria-hidden="true"
        ></div>

        @if (snippetState() === 'ready' && hasSnippet()) {
          <pre
            class="m-0 max-h-full overflow-hidden whitespace-pre-wrap break-words px-3 py-2 font-mono text-[9px] leading-snug text-gray-700 dark:text-gray-300"
          >{{ truncatedSnippet() }}</pre>
        } @else {
          <div class="space-y-1.5 px-3 py-2.5" aria-hidden="true">
            @for (width of skeletonWidths; track $index) {
              <div
                class="h-1.5 rounded-full bg-gray-200 dark:bg-gray-700"
                [style.width.%]="width"
              ></div>
            }
          </div>
        }

        <!-- Bottom fade for long text -->
        <div
          class="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-white to-transparent dark:from-gray-900/40"
          aria-hidden="true"
        ></div>
      </div>

      <!-- Footer -->
      <div class="min-w-0 border-t border-gray-100 px-3 py-2 dark:border-gray-700/60">
        <p class="truncate text-sm font-medium text-gray-900 dark:text-white">
          {{ attachment().filename }}
        </p>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          {{ formattedSize() }}
        </p>
      </div>
    </button>
  `,
})
export class FileAttachmentBadgeComponent {
  readonly attachment = input.required<FileAttachmentData>();

  private readonly fileUploadService = inject(FileUploadService);

  protected readonly skeletonWidths = SKELETON_LINE_WIDTHS;

  protected readonly snippetState = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');
  private readonly snippet = signal<string>('');

  protected readonly formattedSize = computed(() => formatBytes(this.attachment().sizeBytes));

  protected readonly style = computed<FileTypeStyle>(
    () => FILE_TYPE_STYLES[this.attachment().mimeType] ?? DEFAULT_STYLE,
  );

  protected readonly hasSnippet = computed(() => this.snippet().trim().length > 0);

  /** Cap chars so very long unbroken lines don't blow out the card. */
  protected readonly truncatedSnippet = computed(() => {
    const raw = this.snippet();
    return raw.length > 600 ? raw.slice(0, 600) : raw;
  });

  constructor() {
    effect(() => {
      const att = this.attachment();
      if (TEXT_PREVIEW_MIMES.has(att.mimeType)) {
        this.loadSnippet(att.uploadId);
      }
    });
  }

  private async loadSnippet(uploadId: string): Promise<void> {
    this.snippetState.set('loading');
    try {
      const response = await this.fileUploadService.getTextSnippet(uploadId);
      this.snippet.set(response.snippet);
      this.snippetState.set('ready');
    } catch {
      this.snippetState.set('error');
    }
  }

  protected async openFile(): Promise<void> {
    try {
      const response = await this.fileUploadService.getPreviewUrl(this.attachment().uploadId);
      window.open(response.url, '_blank', 'noopener,noreferrer');
    } catch {
      // Silent failure — the broken link state is rare and the message stream
      // surfaces backend errors separately.
    }
  }
}
