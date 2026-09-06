import { Component, ChangeDetectionStrategy, input, output, computed, signal, effect } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroDocument,
  heroDocumentText,
  heroTableCells,
  heroCodeBracket,
  heroXMark,
  heroArrowPath,
  heroExclamationTriangle,
  heroPhoto
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../tooltip';
import { formatBytes, type PendingUpload, type FileMetadata } from '../../services/file-upload';

/**
 * Check if MIME type is an image
 */
function isImageMimeType(mimeType: string): boolean {
  return mimeType.startsWith('image/');
}

/**
 * File type to icon mapping
 */
const FILE_TYPE_ICONS: Record<string, string> = {
  'application/pdf': 'heroDocument',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'heroDocumentText',
  'text/plain': 'heroDocumentText',
  'text/html': 'heroCodeBracket',
  'text/csv': 'heroTableCells',
  'application/vnd.ms-excel': 'heroTableCells',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'heroTableCells',
  'text/markdown': 'heroDocumentText',
  // Images use heroPhoto as fallback when preview is not available
  'image/png': 'heroPhoto',
  'image/jpeg': 'heroPhoto',
  'image/gif': 'heroPhoto',
  'image/webp': 'heroPhoto',
};

/**
 * File type to color mapping for icon container
 * - bg: Brighter background color for visibility
 * - text: Icon color
 * - border: Darker border around icon container
 */
const FILE_TYPE_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  'application/pdf': {
    bg: 'bg-filetype-pdf-100 dark:bg-filetype-pdf-900/60',
    text: 'text-filetype-pdf-600 dark:text-filetype-pdf-300',
    border: 'border-filetype-pdf-300 dark:border-filetype-pdf-700'
  },
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': {
    bg: 'bg-filetype-doc-100 dark:bg-filetype-doc-900/60',
    text: 'text-filetype-doc-600 dark:text-filetype-doc-300',
    border: 'border-filetype-doc-300 dark:border-filetype-doc-700'
  },
  'text/plain': {
    bg: 'bg-gray-100 dark:bg-gray-600',
    text: 'text-gray-600 dark:text-gray-200',
    border: 'border-gray-300 dark:border-gray-500'
  },
  'text/html': {
    bg: 'bg-filetype-code-100 dark:bg-filetype-code-900/60',
    text: 'text-filetype-code-600 dark:text-filetype-code-300',
    border: 'border-filetype-code-300 dark:border-filetype-code-700'
  },
  'text/csv': {
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-600 dark:text-filetype-sheet-300',
    border: 'border-filetype-sheet-300 dark:border-filetype-sheet-700'
  },
  'application/vnd.ms-excel': {
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-600 dark:text-filetype-sheet-300',
    border: 'border-filetype-sheet-300 dark:border-filetype-sheet-700'
  },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': {
    bg: 'bg-filetype-sheet-100 dark:bg-filetype-sheet-900/60',
    text: 'text-filetype-sheet-600 dark:text-filetype-sheet-300',
    border: 'border-filetype-sheet-300 dark:border-filetype-sheet-700'
  },
  'text/markdown': {
    bg: 'bg-filetype-markdown-100 dark:bg-filetype-markdown-900/60',
    text: 'text-filetype-markdown-600 dark:text-filetype-markdown-300',
    border: 'border-filetype-markdown-300 dark:border-filetype-markdown-700'
  },
  // Image types
  'image/png': {
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
    border: 'border-filetype-image-300 dark:border-filetype-image-700'
  },
  'image/jpeg': {
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
    border: 'border-filetype-image-300 dark:border-filetype-image-700'
  },
  'image/gif': {
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
    border: 'border-filetype-image-300 dark:border-filetype-image-700'
  },
  'image/webp': {
    bg: 'bg-filetype-image-100 dark:bg-filetype-image-900/60',
    text: 'text-filetype-image-600 dark:text-filetype-image-300',
    border: 'border-filetype-image-300 dark:border-filetype-image-700'
  },
};

const DEFAULT_COLORS = {
  bg: 'bg-gray-100 dark:bg-gray-600',
  text: 'text-gray-600 dark:text-gray-200',
  border: 'border-gray-300 dark:border-gray-500'
};

/**
 * Compact file card component for displaying file attachments.
 *
 * Supports two modes:
 * 1. Pending upload mode - shows progress, status, retry/cancel options
 * 2. Ready file mode - shows file info with delete option
 *
 * @example
 * ```html
 * <!-- Pending upload -->
 * <app-file-card
 *   [pendingUpload]="upload"
 *   (remove)="onRemove($event)"
 *   (retry)="onRetry($event)"
 * />
 *
 * <!-- Ready file -->
 * <app-file-card
 *   [file]="fileMetadata"
 *   (remove)="onDelete($event)"
 * />
 * ```
 */
@Component({
  selector: 'app-file-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroDocument,
      heroDocumentText,
      heroTableCells,
      heroCodeBracket,
      heroXMark,
      heroArrowPath,
      heroExclamationTriangle,
      heroPhoto
    })
  ],
  host: {
    'class': 'contents'
  },
  template: `
    <div
      class="group relative flex w-56 shrink-0 items-center gap-2 rounded-lg border px-3 py-2 transition-colors"
      [class]="containerClass()"
    >
      <!-- File icon or image preview -->
      <div
        class="flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md border"
        [class]="iconContainerClass()"
      >
        @if (isError()) {
          <ng-icon name="heroExclamationTriangle" class="size-6 text-state-danger-500" aria-hidden="true" />
        } @else if (isUploading()) {
          <ng-icon name="heroArrowPath" class="size-6 animate-spin" [class]="iconClass()" aria-hidden="true" />
        } @else if (isImage() && imagePreviewUrl()) {
          <!-- Image thumbnail preview -->
          <img
            [src]="imagePreviewUrl()"
            [alt]="fileName()"
            class="size-10 object-cover"
          />
        } @else {
          <ng-icon [name]="iconName()" class="size-6" [class]="iconClass()" aria-hidden="true" />
        }
      </div>

      <!-- File info -->
      <div class="min-w-0 flex-1">
        <p class="truncate text-sm font-medium text-gray-900 dark:text-white">
          {{ fileName() }}
        </p>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          @if (isError()) {
            <span class="text-state-danger-500 dark:text-state-danger-400">{{ errorMessage() }}</span>
          } @else if (isUploading()) {
            Uploading... {{ progress() }}%
          } @else if (isCompleting()) {
            Finalizing...
          } @else {
            {{ formattedSize() }}
          }
        </p>
      </div>

      <!-- Progress bar (during upload) -->
      @if (isUploading() && progress() < 100) {
        <div class="absolute bottom-0 left-0 right-0 h-0.5 overflow-hidden rounded-b-lg bg-gray-200 dark:bg-gray-700">
          <div
            class="h-full bg-primary-500 transition-all duration-200"
            [style.width.%]="progress()"
          ></div>
        </div>
      }

      <!-- Action buttons -->
      <div class="flex shrink-0 items-center gap-1">
        @if (isError() && pendingUpload()) {
          <!-- Retry button -->
          <button
            type="button"
            (click)="onRetryClick($event)"
            class="flex size-6 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-gray-300"
            [appTooltip]="'Retry upload'"
            appTooltipPosition="top"
          >
            <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
            <span class="sr-only">Retry upload</span>
          </button>
        }

        <!-- Remove button -->
        <button
          type="button"
          (click)="onRemoveClick($event)"
          class="flex size-6 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
          [appTooltip]="removeTooltip()"
          appTooltipPosition="top"
        >
          <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          <span class="sr-only">{{ removeTooltip() }}</span>
        </button>
      </div>
    </div>
  `
})
export class FileCardComponent {
  /** Pending upload data (for uploads in progress) */
  readonly pendingUpload = input<PendingUpload | null>(null);

  /** File metadata (for ready files) */
  readonly file = input<FileMetadata | null>(null);

  /** Emitted when remove/cancel is clicked */
  readonly remove = output<string>();

  /** Emitted when retry is clicked (pending uploads only) */
  readonly retry = output<PendingUpload>();

  /** Image preview URL (data URL for images) */
  readonly imagePreviewUrl = signal<string | null>(null);

  constructor() {
    // Effect to load image preview when pendingUpload changes
    effect(() => {
      const pending = this.pendingUpload();
      if (pending && isImageMimeType(pending.file.type)) {
        this.loadImagePreview(pending.file);
      } else {
        this.imagePreviewUrl.set(null);
      }
    });
  }

  /**
   * Load image preview from file using FileReader
   */
  private loadImagePreview(file: File): void {
    const reader = new FileReader();
    reader.onload = (e) => {
      this.imagePreviewUrl.set(e.target?.result as string);
    };
    reader.onerror = () => {
      this.imagePreviewUrl.set(null);
    };
    reader.readAsDataURL(file);
  }

  // Computed values
  protected readonly fileName = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return pending.file.name;

    const file = this.file();
    if (file) return file.filename;

    return 'Unknown file';
  });

  protected readonly mimeType = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return pending.file.type;

    const file = this.file();
    if (file) return file.mimeType;

    return 'application/octet-stream';
  });

  protected readonly sizeBytes = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return pending.file.size;

    const file = this.file();
    if (file) return file.sizeBytes;

    return 0;
  });

  protected readonly formattedSize = computed(() => formatBytes(this.sizeBytes()));

  protected readonly uploadId = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return pending.uploadId;

    const file = this.file();
    if (file) return file.uploadId;

    return '';
  });

  protected readonly status = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return pending.status;

    const file = this.file();
    if (file) return file.status;

    return 'ready';
  });

  protected readonly progress = computed(() => {
    const pending = this.pendingUpload();
    return pending?.progress ?? 100;
  });

  protected readonly errorMessage = computed(() => {
    const pending = this.pendingUpload();
    return pending?.error ?? 'Upload failed';
  });

  protected readonly isUploading = computed(() => this.status() === 'uploading');
  protected readonly isCompleting = computed(() => this.status() === 'completing');
  protected readonly isError = computed(() => this.status() === 'error');
  protected readonly isReady = computed(() => this.status() === 'ready');
  protected readonly isImage = computed(() => isImageMimeType(this.mimeType()));

  protected readonly iconName = computed(() => {
    const mime = this.mimeType();
    return FILE_TYPE_ICONS[mime] ?? 'heroDocument';
  });

  protected readonly colors = computed(() => {
    const mime = this.mimeType();
    return FILE_TYPE_COLORS[mime] ?? DEFAULT_COLORS;
  });

  protected readonly containerClass = computed(() => {
    if (this.isError()) {
      return 'border-state-danger-200 bg-state-danger-50 dark:border-state-danger-800 dark:bg-state-danger-950/30';
    }
    return 'border-gray-200 bg-gray-50 dark:border-gray-600 dark:bg-gray-800';
  });

  protected readonly iconContainerClass = computed(() => {
    const colors = this.colors();
    if (this.isError()) {
      return 'bg-state-danger-100 border-state-danger-300 dark:bg-state-danger-900/50 dark:border-state-danger-700';
    }
    return `${colors.bg} ${colors.border}`;
  });

  protected readonly iconClass = computed(() => {
    const colors = this.colors();
    if (this.isError()) {
      return 'text-state-danger-500 dark:text-state-danger-400';
    }
    return colors.text;
  });

  protected readonly removeTooltip = computed(() => {
    if (this.isUploading() || this.isCompleting()) {
      return 'Cancel upload';
    }
    if (this.isError()) {
      return 'Remove';
    }
    return 'Delete file';
  });

  protected onRemoveClick(event: Event): void {
    event.stopPropagation();
    const id = this.uploadId();
    if (id) {
      this.remove.emit(id);
    }
  }

  protected onRetryClick(event: Event): void {
    event.stopPropagation();
    const pending = this.pendingUpload();
    if (pending) {
      this.retry.emit(pending);
    }
  }
}
