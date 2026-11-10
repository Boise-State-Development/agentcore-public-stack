import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroPresentationChartBar,
  heroTableCells,
  heroXMark,
} from '@ng-icons/heroicons/outline';

import {
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
} from '../../../components/content-card/content-card.component';
import {
  AttachmentSearchStatusService,
  FileUploadService,
  formatBytes,
  isReferenceDocument,
  largeDocumentChip,
  LARGE_DOCUMENT_CHIP_COPY,
  resolveMimeType,
  type FileMetadata,
  type LargeDocumentChip,
  type PendingUpload,
} from '../../../services/file-upload';
import { FEATURES } from '../../../services/features';
import { FilePreviewStateService } from '../../services/file-preview/file-preview-state.service';
import { isPreviewableFilename } from '../../services/file-preview/file-preview.model';
import type { FileAttachmentData } from '../../services/models/message.model';
import { MarkdownPreviewModalComponent } from '../message-list/components/file-attachment/markdown-preview-modal.component';
import { attachmentKind } from './attachment-kind';
import { AttachmentPreviewComponent } from './attachment-preview.component';

/** Where the card sits, which decides its size and its actions. */
export type AttachmentCardSurface = 'composer' | 'message';

/** The card's state, from whichever of its three sources it was given. */
export type AttachmentCardState = 'uploading' | 'preparing' | 'ready' | 'failed';

/**
 * One attached file, drawn with the shared content card: its preview in the
 * tray, then the name and a `TYPE · size · status` line.
 *
 * It takes exactly one source. In the composer that is a `PendingUpload`
 * (this tab's upload, with progress) or a `FileMetadata` (a file restored with
 * a draft); in a sent message it is the message's `FileAttachmentData`.
 *
 * - Composer: small, with a remove button on the tray that is always visible
 *   (touch has no hover), a bar along the tray while bytes move, and Retry on
 *   a failed upload.
 * - Message: conversation size, with Preview where the docked pane or the
 *   Markdown modal can show the file and Open (a new tab) otherwise.
 *
 * A large document attached by reference says so in the status line, and
 * with the attachments KB follows its index (indexing, then searchable).
 */
@Component({
  selector: 'app-attachment-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, AttachmentPreviewComponent, MarkdownPreviewModalComponent, NgIcon],
  providers: [
    provideIcons({
      heroArrowPath,
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroPresentationChartBar,
      heroTableCells,
      heroXMark,
    }),
  ],
  host: { class: 'block min-w-0', '[class]': 'hostClass()' },
  template: `
    <app-content-card
      class="h-full"
      [title]="name()"
      [label]="cardLabel()"
      [meta]="meta()"
      [status]="statusText()"
      [live]="state() === 'uploading' || state() === 'preparing'"
      [tone]="state() === 'failed' ? 'danger' : 'default'"
      [size]="surface() === 'composer' ? 'sm' : 'md'"
      [bleed]="kind().preview === 'image'"
      [progress]="progressBar()"
    >
      <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1.5">
        <span class="flex shrink-0" [class]="kind().accent" aria-hidden="true">
          <ng-icon [name]="kind().icon" size="13" />
        </span>
        <span
          class="min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-600 dark:bg-white/5 dark:text-gray-400"
          aria-hidden="true"
          >{{ kind().name }}</span
        >
      </div>

      <app-attachment-preview
        contentCardPreview
        [class.opacity-75]="state() === 'uploading'"
        [class.grayscale]="state() === 'failed'"
        [class.opacity-50]="state() === 'failed'"
        [uploadId]="uploadId()"
        [mimeType]="mimeType()"
        [file]="localFile()"
        [serverReady]="state() === 'ready'"
        [preparing]="state() === 'preparing'"
      />

      @if (surface() === 'composer') {
        <button
          contentCardCorner
          type="button"
          class="flex size-6 items-center justify-center rounded-full bg-white text-gray-600 shadow-sm ring-1 ring-gray-200 transition-colors hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:bg-gray-900 dark:text-gray-300 dark:ring-white/15 dark:hover:text-state-danger-400"
          [attr.aria-label]="removeLabel()"
          (click)="onRemove($event)"
        >
          <ng-icon name="heroXMark" size="14" aria-hidden="true" />
        </button>
      }

      @if (state() === 'failed' && pendingUpload()) {
        <button
          contentCardAction
          type="button"
          class="inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-0.5 text-xs/5 font-medium text-state-danger-700 ring-1 ring-state-danger-300 hover:bg-state-danger-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-state-danger-300 dark:ring-state-danger-800 dark:hover:bg-state-danger-950/40"
          [attr.aria-label]="'Retry upload of ' + name()"
          (click)="onRetry($event)"
        >
          <ng-icon name="heroArrowPath" size="13" aria-hidden="true" />
          Retry
        </button>
      } @else if (surface() === 'message') {
        <button
          contentCardAction
          type="button"
          [class]="actionClass"
          [attr.aria-label]="openLabel() + ' ' + name()"
          (click)="open()"
        >
          {{ openLabel() }}
        </button>
      }
    </app-content-card>

    @if (markdownModalOpen()) {
      <app-markdown-preview-modal
        [uploadId]="uploadId()"
        [filename]="name()"
        (close)="markdownModalOpen.set(false)"
      />
    }
  `,
})
export class AttachmentCardComponent {
  /** This tab's upload, while it is in flight or after it failed. */
  readonly pendingUpload = input<PendingUpload | null>(null);
  /** A file restored into the composer with a draft. */
  readonly file = input<FileMetadata | null>(null);
  /** A file on a sent message. */
  readonly attachment = input<FileAttachmentData | null>(null);
  readonly surface = input<AttachmentCardSurface>('composer');
  /** Fill the width the layout gives it, rather than the card's own. */
  readonly fill = input(false);

  /** Remove (or cancel) this file; carries its upload id. */
  readonly remove = output<string>();
  readonly retry = output<PendingUpload>();

  private readonly fileUploadService = inject(FileUploadService);
  private readonly filePreview = inject(FilePreviewStateService);
  /** Present only while the deployment has the attachments KB. */
  private readonly searchStatus = inject(FEATURES).attachmentKb ? inject(AttachmentSearchStatusService) : null;

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;
  protected readonly markdownModalOpen = signal(false);

  protected readonly hostClass = computed(() => {
    if (this.fill()) return 'w-full';
    return this.surface() === 'composer' ? 'w-54 shrink-0' : 'w-72 max-w-full';
  });

  protected readonly name = computed(
    () => this.pendingUpload()?.file.name ?? this.file()?.filename ?? this.attachment()?.filename ?? 'File',
  );

  protected readonly mimeType = computed(() => {
    const pending = this.pendingUpload();
    if (pending) return resolveMimeType(pending.file);
    return this.file()?.mimeType ?? this.attachment()?.mimeType ?? 'application/octet-stream';
  });

  protected readonly sizeBytes = computed(
    () => this.pendingUpload()?.file.size ?? this.file()?.sizeBytes ?? this.attachment()?.sizeBytes ?? 0,
  );

  protected readonly uploadId = computed(
    () => this.pendingUpload()?.uploadId ?? this.file()?.uploadId ?? this.attachment()?.uploadId ?? '',
  );

  protected readonly localFile = computed(() => this.pendingUpload()?.file ?? null);

  protected readonly kind = computed(() => attachmentKind(this.mimeType()));

  readonly state = computed<AttachmentCardState>(() => {
    const pending = this.pendingUpload();
    if (pending) {
      if (pending.status === 'uploading') return 'uploading';
      if (pending.status === 'completing') return 'preparing';
      if (pending.status === 'error') return 'failed';
      return 'ready';
    }
    const file = this.file();
    if (file) {
      if (file.status === 'failed') return 'failed';
      if (file.status === 'pending') return 'preparing';
    }
    return 'ready';
  });

  /** The bar along the tray: only while bytes move and there is a percentage. */
  protected readonly progressBar = computed(() => {
    const pending = this.pendingUpload();
    if (!pending || pending.status !== 'uploading' || pending.statusLabel) return null;
    return pending.progress;
  });

  protected readonly isReference = computed(() => isReferenceDocument(this.mimeType(), this.sizeBytes()));

  protected readonly chip = computed<LargeDocumentChip>(() => {
    const service = this.searchStatus;
    if (!service || this.state() !== 'ready') return 'read-on-demand';
    return largeDocumentChip(service.statusFor(this.uploadId()));
  });

  protected readonly meta = computed(() => {
    if (this.state() === 'failed') return '';
    if (this.state() === 'uploading') return this.kind().label;
    return `${this.kind().label} · ${formatBytes(this.sizeBytes())}`;
  });

  protected readonly statusText = computed(() => {
    switch (this.state()) {
      case 'uploading': {
        const pending = this.pendingUpload();
        return pending?.statusLabel ?? `Uploading ${pending?.progress ?? 0}%`;
      }
      case 'preparing':
        return 'Preparing…';
      case 'failed':
        return this.pendingUpload()?.error ?? 'Upload failed';
      default:
        return this.isReference() ? LARGE_DOCUMENT_CHIP_COPY[this.chip()].short : '';
    }
  });

  /** The accessible name says in full what the status line abbreviates. */
  protected readonly cardLabel = computed(() => {
    const reference =
      this.state() === 'ready' && this.isReference() ? `, ${LARGE_DOCUMENT_CHIP_COPY[this.chip()].full}` : '';
    return `${this.name()}${reference}`;
  });

  protected readonly removeLabel = computed(() =>
    this.state() === 'uploading' || this.state() === 'preparing'
      ? `Cancel upload of ${this.name()}`
      : `Remove ${this.name()}`,
  );

  private readonly isMarkdown = computed(() => this.mimeType() === 'text/markdown');
  private readonly isPanePreviewable = computed(() => isPreviewableFilename(this.name()));

  protected readonly openLabel = computed(() =>
    this.isPanePreviewable() || this.isMarkdown() ? 'Preview' : 'Open',
  );

  constructor() {
    // A ready large document follows its search index (one poll per upload,
    // shared by every card showing it).
    effect(() => {
      const service = this.searchStatus;
      if (!service || !this.isReference() || this.state() !== 'ready') return;
      const id = this.uploadId();
      untracked(() => service.track(id));
    });
  }

  protected onRemove(event: Event): void {
    event.stopPropagation();
    const id = this.uploadId();
    if (id) this.remove.emit(id);
  }

  protected onRetry(event: Event): void {
    event.stopPropagation();
    const pending = this.pendingUpload();
    if (pending) this.retry.emit(pending);
  }

  /**
   * Markdown opens in its modal; anything the docked pane renders (.docx,
   * .pptx, PDF) opens there, the same gate the generated-file card uses; the
   * rest opens in a new tab from a short-lived presigned URL.
   */
  protected async open(): Promise<void> {
    if (this.isMarkdown()) {
      this.markdownModalOpen.set(true);
      return;
    }
    if (this.isPanePreviewable()) {
      this.filePreview.open({ uploadId: this.uploadId(), filename: this.name() });
      return;
    }
    try {
      const response = await this.fileUploadService.getPreviewUrl(this.uploadId());
      window.open(response.url, '_blank', 'noopener,noreferrer');
    } catch {
      // Rare, and the message stream surfaces backend errors separately.
    }
  }
}
