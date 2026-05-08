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
import { MarkdownComponent } from 'ngx-markdown';
import { formatBytes, FileUploadService } from '../../../../../services/file-upload';
import { FileAttachmentData } from '../../../../services/models/message.model';
import { MarkdownPreviewModalComponent } from './markdown-preview-modal.component';

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

/** MIME types where the backend can produce a real first-page thumbnail. */
const THUMBNAIL_PREVIEW_MIMES = new Set(['application/pdf']);

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
  imports: [NgIcon, MarkdownComponent, MarkdownPreviewModalComponent],
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

    /* Compact markdown styling for the small in-card preview. The card body
       is only ~128px tall so we shrink everything aggressively and strip the
       margins that the global .message-block prose styles add. */
    .md-card-preview {
      font-size: 9px;
      line-height: 1.45;
      color: rgb(55 65 81);
    }
    :host-context(.dark) .md-card-preview {
      color: rgb(209 213 219);
    }
    .md-card-preview :is(h1, h2, h3, h4, h5, h6) {
      font-weight: 700;
      line-height: 1.25;
      margin: 0 0 2px;
      color: rgb(17 24 39);
    }
    :host-context(.dark) .md-card-preview :is(h1, h2, h3, h4, h5, h6) {
      color: rgb(243 244 246);
    }
    .md-card-preview h1 { font-size: 12px; }
    .md-card-preview h2 { font-size: 11px; }
    .md-card-preview h3,
    .md-card-preview h4,
    .md-card-preview h5,
    .md-card-preview h6 { font-size: 10px; }
    .md-card-preview p { margin: 0 0 4px; }
    .md-card-preview ul,
    .md-card-preview ol {
      margin: 0 0 4px;
      padding-left: 14px;
    }
    .md-card-preview li { margin: 0 0 1px; }
    .md-card-preview code {
      font-size: 8.5px;
      background: rgb(243 244 246);
      padding: 0 2px;
      border-radius: 2px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .md-card-preview pre {
      font-size: 8.5px;
      background: rgb(243 244 246);
      padding: 4px;
      border-radius: 4px;
      overflow: hidden;
      margin: 0 0 4px;
    }
    :host-context(.dark) .md-card-preview code,
    :host-context(.dark) .md-card-preview pre {
      background: rgb(31 41 55);
    }
    .md-card-preview a {
      color: rgb(99 102 241);
      text-decoration: underline;
    }
    .md-card-preview strong { font-weight: 600; }
    .md-card-preview blockquote {
      border-left: 2px solid rgb(209 213 219);
      padding-left: 6px;
      margin: 0 0 4px;
      color: rgb(107 114 128);
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

        @if (thumbnailUrl(); as url) {
          <img
            [src]="url"
            [alt]="'First page of ' + attachment().filename"
            class="size-full object-cover object-top"
            loading="lazy"
            decoding="async"
          />
        } @else if (snippetState() === 'ready' && hasSnippet()) {
          @if (isMarkdown()) {
            <div class="md-card-preview h-full overflow-hidden px-3 py-2">
              <markdown [data]="truncatedSnippet()" />
            </div>
          } @else {
            <pre
              class="m-0 max-h-full overflow-hidden whitespace-pre-wrap break-words px-3 py-2 font-mono text-[9px] leading-snug text-gray-700 dark:text-gray-300"
            >{{ truncatedSnippet() }}</pre>
          }
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

        <!-- Bottom fade for long text. Suppressed when a thumbnail is shown
             so the rendered page edge stays crisp. -->
        @if (!thumbnailUrl()) {
          <div
            class="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-white to-transparent dark:from-gray-900/40"
            aria-hidden="true"
          ></div>
        }
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

    @if (markdownModalOpen()) {
      <app-markdown-preview-modal
        [uploadId]="attachment().uploadId"
        [filename]="attachment().filename"
        (close)="markdownModalOpen.set(false)"
      />
    }
  `,
})
export class FileAttachmentBadgeComponent {
  readonly attachment = input.required<FileAttachmentData>();

  private readonly fileUploadService = inject(FileUploadService);

  protected readonly skeletonWidths = SKELETON_LINE_WIDTHS;

  protected readonly snippetState = signal<'idle' | 'loading' | 'ready' | 'error'>('idle');
  private readonly snippet = signal<string>('');
  protected readonly markdownModalOpen = signal(false);

  /** Presigned URL for a real first-page thumbnail (PDFs today). null on
      unsupported types or render failure — caller falls back to skeleton. */
  protected readonly thumbnailUrl = signal<string | null>(null);

  protected readonly formattedSize = computed(() => formatBytes(this.attachment().sizeBytes));

  protected readonly style = computed<FileTypeStyle>(
    () => FILE_TYPE_STYLES[this.attachment().mimeType] ?? DEFAULT_STYLE,
  );

  protected readonly hasSnippet = computed(() => this.snippet().trim().length > 0);

  protected readonly isMarkdown = computed(() => this.attachment().mimeType === 'text/markdown');

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
      if (THUMBNAIL_PREVIEW_MIMES.has(att.mimeType)) {
        this.loadThumbnail(att.uploadId);
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

  private async loadThumbnail(uploadId: string): Promise<void> {
    const result = await this.fileUploadService.getThumbnail(uploadId);
    this.thumbnailUrl.set(result.status === 'ready' ? result.response.url : null);
  }

  protected async openFile(): Promise<void> {
    if (this.isMarkdown()) {
      this.markdownModalOpen.set(true);
      return;
    }
    try {
      const response = await this.fileUploadService.getPreviewUrl(this.attachment().uploadId);
      window.open(response.url, '_blank', 'noopener,noreferrer');
    } catch {
      // Silent failure — the broken link state is rare and the message stream
      // surfaces backend errors separately.
    }
  }
}
