import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  ViewEncapsulation,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { MarkdownComponent } from 'ngx-markdown';

import { FileUploadService } from '../../../services/file-upload';
import {
  type AttachmentPreviewKind,
  attachmentKind,
  csvPreviewRows,
  LOCAL_SNIPPET_BYTES,
  TEXT_PREVIEW_KINDS,
} from './attachment-kind';

/** Skeleton "lines of text" widths (percent), tuned to look like a paragraph. */
const LINE_WIDTHS = [92, 78, 88, 64, 95, 70, 84, 58];
/** Bullet-row widths (percent) inside the mock slide. */
const BULLET_WIDTHS = [78, 92, 60];
/** Characters of a snippet drawn, so a long unbroken line can't blow out the card. */
const SNIPPET_CHARS = 600;

/**
 * The inside of an attachment card's sheet: the file's first page, opening
 * lines, first rows or a type-shaped skeleton.
 *
 * Every source here exists today. In the composer a local file previews from
 * its own bytes (`FileReader` for an image, `File.slice` for text) before the
 * upload finishes; once the server has the file, a PDF's first page comes
 * from `GET /files/{id}/thumbnail` and text from `getTextSnippet`. Word,
 * spreadsheet and deck files draw a skeleton of their shape until the
 * prepare step writes a preview for them.
 *
 * Display only: nothing here reaches a model call or the stored message.
 * Decorative, so hidden from assistive tech; the card's footer names the file.
 */
@Component({
  selector: 'app-attachment-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  // Unscoped: ngx-markdown writes its HTML outside this component's style
  // scope, so scoped rules never reach the headings. Every class is prefixed.
  encapsulation: ViewEncapsulation.None,
  imports: [MarkdownComponent, NgTemplateOutlet],
  host: { class: 'relative block size-full', 'aria-hidden': 'true' },
  styles: `
    .attachment-md-preview {
      font-size: 9px;
      line-height: 1.45;
    }
    .attachment-md-preview :is(h1, h2, h3, h4, h5, h6) {
      font-weight: 700;
      color: inherit;
      line-height: 1.25;
      margin: 0 0 2px;
    }
    .attachment-md-preview h1 { font-size: 12px; }
    .attachment-md-preview h2 { font-size: 11px; }
    .attachment-md-preview :is(h3, h4, h5, h6) { font-size: 10px; }
    .attachment-md-preview :is(p, ul, ol, pre, blockquote) { margin: 0 0 4px; }
    .attachment-md-preview :is(ul, ol) { padding-left: 14px; }
    .attachment-md-preview :is(code, pre) { font-size: 8.5px; }
    .attachment-md-preview strong { font-weight: 600; }

    /* Preparing: a soft sweep across the sheet, still under reduced motion. */
    .attachment-shimmer {
      background: linear-gradient(100deg, transparent 30%, rgb(255 255 255 / 0.55) 50%, transparent 70%);
      background-size: 250% 100%;
    }
    .dark .attachment-shimmer {
      background-image: linear-gradient(100deg, transparent 30%, rgb(255 255 255 / 0.08) 50%, transparent 70%);
    }
    @media (prefers-reduced-motion: no-preference) {
      .attachment-shimmer { animation: attachment-sweep 1.8s linear infinite; }
    }
    @keyframes attachment-sweep {
      from { background-position: 120% 0; }
      to { background-position: -120% 0; }
    }
  `,
  template: `
    @switch (view()) {
      @case ('image') {
        @if (imageUrl(); as url) {
          <img class="size-full object-cover" [src]="url" alt="" (error)="imageUrl.set(null)" />
        } @else {
          <div class="size-full bg-gray-200 dark:bg-gray-700"></div>
        }
      }
      @case ('thumbnail') {
        <img
          class="size-full object-cover object-top"
          [src]="thumbnailUrl()"
          alt=""
          loading="lazy"
          decoding="async"
          (load)="thumbnailReminted.set(false)"
          (error)="onThumbnailError()"
        />
      }
      @case ('markdown') {
        <div class="attachment-md-preview h-full overflow-hidden px-3 py-2 text-gray-700 dark:text-gray-300">
          <markdown [data]="snippet()" />
        </div>
        <ng-container [ngTemplateOutlet]="fade" />
      }
      @case ('text') {
        <pre
          class="m-0 h-full overflow-hidden whitespace-pre-wrap break-words px-3 py-2 font-mono text-[9px] leading-snug text-gray-700 dark:text-gray-300"
        >{{ snippet() }}</pre>
        <ng-container [ngTemplateOutlet]="fade" />
      }
      @case ('csv') {
        <table class="w-full table-fixed border-collapse text-[9px] leading-4 text-gray-700 dark:text-gray-300">
          <tbody>
            @for (row of csvRows(); track $index; let first = $first) {
              <tr class="border-b border-gray-100 dark:border-white/5" [class.font-semibold]="first">
                @for (cell of row; track $index) {
                  <td class="truncate px-1.5 py-0.5">{{ cell }}</td>
                }
              </tr>
            }
          </tbody>
        </table>
        <ng-container [ngTemplateOutlet]="fade" />
      }
      @case ('sheet') {
        <div class="grid h-full grid-cols-4 grid-rows-6">
          @for (cell of sheetCells; track $index) {
            <div class="border-b border-r border-gray-100 p-1 dark:border-white/5">
              @if (cell) {
                <div
                  class="h-1 rounded-full"
                  [class]="$index < 4 ? 'bg-filetype-sheet-300 dark:bg-filetype-sheet-800' : 'bg-gray-200 dark:bg-gray-700'"
                  [style.width.%]="cell"
                ></div>
              }
            </div>
          }
        </div>
      }
      @case ('deck') {
        <div class="flex h-full items-center justify-center">
          <div
            class="flex aspect-video h-[78%] flex-col rounded-md border border-gray-200 bg-white p-2 shadow-sm dark:border-gray-600 dark:bg-gray-800"
          >
            <div class="h-1.5 w-3/5 rounded-full bg-filetype-presentation-600/80"></div>
            <div class="mt-1.5 space-y-1">
              @for (width of bulletWidths; track $index) {
                <div class="flex items-center gap-1">
                  <div class="size-1 shrink-0 rounded-full bg-filetype-presentation-400/70"></div>
                  <div class="h-1 rounded-full bg-gray-200 dark:bg-gray-600" [style.width.%]="width"></div>
                </div>
              }
            </div>
          </div>
        </div>
      }
      @default {
        <div class="space-y-1.5 px-3 py-2.5">
          @for (width of lineWidths; track $index) {
            <div class="h-1.5 rounded-full bg-gray-200 dark:bg-gray-700" [style.width.%]="width"></div>
          }
        </div>
      }
    }

    @if (preparing()) {
      <div class="attachment-shimmer pointer-events-none absolute inset-0"></div>
    }

    <ng-template #fade>
      <div
        class="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-white to-transparent dark:from-gray-950"
      ></div>
    </ng-template>
  `,
})
export class AttachmentPreviewComponent {
  readonly uploadId = input.required<string>();
  readonly mimeType = input.required<string>();
  /** The local file, while the composer still holds it. */
  readonly file = input<File | null>(null);
  /** The server has the file, so its routes can be asked. */
  readonly serverReady = input(true);
  /** Draw the preparing shimmer over the preview. */
  readonly preparing = input(false);

  private readonly fileUploadService = inject(FileUploadService);

  protected readonly lineWidths = LINE_WIDTHS;
  protected readonly bulletWidths = BULLET_WIDTHS;
  protected readonly sheetCells = [
    72, 64, 80, 56, 48, 0, 66, 40, 70, 52, 0, 44, 58, 76, 36, 0, 62, 0, 50, 68, 0, 46, 60, 0,
  ];

  protected readonly imageUrl = signal<string | null>(null);
  protected readonly thumbnailUrl = signal<string | null>(null);
  protected readonly thumbnailReminted = signal(false);
  private readonly text = signal('');

  private readonly kind = computed(() => attachmentKind(this.mimeType()).preview);

  protected readonly snippet = computed(() => this.text().slice(0, SNIPPET_CHARS));
  protected readonly csvRows = computed(() => csvPreviewRows(this.text()));

  protected readonly view = computed(() => {
    const kind = this.kind();
    if (kind === 'image') return 'image';
    if (kind === 'pdf') return this.thumbnailUrl() ? 'thumbnail' : 'lines';
    if (TEXT_PREVIEW_KINDS.has(kind)) return this.text().trim() ? kind : 'lines';
    if (kind === 'deck' || kind === 'sheet') return kind;
    return 'lines';
  });

  constructor() {
    effect(() => {
      const kind = this.kind();
      const file = this.file();
      const id = this.uploadId();
      const ready = this.serverReady();
      untracked(() => this.load(kind, id, file, ready));
    });
  }

  private load(kind: AttachmentPreviewKind, uploadId: string, file: File | null, serverReady: boolean): void {
    if (kind === 'image') {
      if (file) this.readImage(file);
      else if (serverReady) void this.fetchImage(uploadId);
      return;
    }
    if (TEXT_PREVIEW_KINDS.has(kind)) {
      if (file) void this.readText(file);
      else if (serverReady) void this.fetchText(uploadId);
      return;
    }
    if (kind === 'pdf' && serverReady) void this.fetchThumbnail(uploadId);
  }

  private readImage(file: File): void {
    const reader = new FileReader();
    reader.onload = () => this.imageUrl.set(typeof reader.result === 'string' ? reader.result : null);
    reader.onerror = () => this.imageUrl.set(null);
    reader.readAsDataURL(file);
  }

  private async fetchImage(uploadId: string): Promise<void> {
    try {
      this.imageUrl.set((await this.fileUploadService.getPreviewUrl(uploadId)).url);
    } catch {
      this.imageUrl.set(null);
    }
  }

  private async readText(file: File): Promise<void> {
    try {
      this.text.set(await file.slice(0, LOCAL_SNIPPET_BYTES).text());
    } catch {
      this.text.set('');
    }
  }

  private async fetchText(uploadId: string): Promise<void> {
    try {
      this.text.set((await this.fileUploadService.getTextSnippet(uploadId)).snippet);
    } catch {
      this.text.set('');
    }
  }

  private async fetchThumbnail(uploadId: string): Promise<void> {
    const result = await this.fileUploadService.getThumbnailWhenRendered(uploadId);
    this.thumbnailUrl.set(result.status === 'ready' ? result.response.url : null);
  }

  /**
   * The thumbnail failed to load. Its presigned URL lives ~10 minutes and the
   * image is lazy, so a card scrolled back into view later asks S3 with a dead
   * signature. Re-mint once, then fall back to the skeleton.
   */
  protected onThumbnailError(): void {
    const retry = !this.thumbnailReminted();
    this.thumbnailUrl.set(null);
    if (!retry) return;
    this.thumbnailReminted.set(true);
    void this.fetchThumbnail(this.uploadId());
  }
}
