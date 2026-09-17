import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroArrowPath,
  heroExclamationTriangle,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { DockedPaneService } from '../../../../services/docked-pane/docked-pane.service';
import { FilePreviewStateService } from '../../../../services/file-preview/file-preview-state.service';
import {
  FilePreviewError,
  FilePreviewHttpService,
} from '../../../../services/file-preview/file-preview-http.service';
import { ConfigService } from '../../../../../services/config.service';
import { downloadUrlFor } from '../../../../../shared/utils/file-download-url';
import { TooltipDirective } from '../../../../../components/tooltip/tooltip.directive';
import { CsvViewerComponent } from './csv-viewer.component';
import { DocxViewerComponent } from './docx-viewer.component';
import { PptxViewerComponent } from './pptx-viewer.component';
import { XlsxViewerComponent } from './xlsx-viewer.component';
import {
  PREVIEW_KIND_LABELS,
  PreviewKind,
  previewFetchesBytes,
  previewKindFor,
} from '../../../../services/file-preview/file-preview.model';

/**
 * Right-docked pane that previews one uploaded file in the browser —
 * `.docx`, `.pptx`, `.csv` and `.xlsx` today.
 *
 * Three of those four are read here from bytes the pane fetched through
 * a presigned URL. `.xlsx` is not: no client-side workbook reader was
 * shippable, so app-api reads it and its viewer takes an upload id
 * instead. `previewFetchesBytes` is what keeps the two paths from
 * treading on each other.
 *
 * Shares the rail with `ArtifactPanelComponent` through
 * `DockedPaneService` — same width, same resize affordance, same
 * side-nav choreography — but deliberately not the same component. The
 * artifact panel's chrome is built around versions, sharing, rename and
 * delete, none of which a previewed file has; and its body is a
 * credential-bearing iframe onto the isolated artifact origin, which is
 * exactly the machinery a `.docx` does not need. A file is inert bytes
 * we fetched ourselves and render in-process, so it gets the simpler
 * pane rather than a conditional inside the complicated one.
 *
 * Download stays available while the preview is loading or broken: the
 * durable `/files/{id}/download` route is a plain link that works
 * regardless of whether the renderer could read the file, and a user
 * whose document failed to preview still wants to open it in Word.
 */
@Component({
  selector: 'app-file-preview-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    TooltipDirective,
    CsvViewerComponent,
    DocxViewerComponent,
    PptxViewerComponent,
    XlsxViewerComponent,
  ],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroArrowPath,
      heroExclamationTriangle,
      heroXMark,
    }),
  ],
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    @if (open(); as ref) {
      <aside
        class="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900"
        [style.maxWidth]="paneWidthCss()"
        [class.select-none]="dragging()"
        [attr.aria-label]="'File preview: ' + ref.filename"
      >
        <div
          role="separator"
          aria-orientation="vertical"
          tabindex="0"
          aria-label="Resize preview panel"
          [attr.aria-valuemin]="paneWidthMin"
          [attr.aria-valuemax]="paneWidthMax"
          [attr.aria-valuenow]="paneWidth()"
          class="group absolute inset-y-0 left-0 z-10 flex w-2 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center focus-visible:outline-none"
          (pointerdown)="onHandlePointerDown($event)"
          (pointermove)="onHandlePointerMove($event)"
          (pointerup)="onHandlePointerUp($event)"
          (pointercancel)="onHandlePointerUp($event)"
          (keydown)="onHandleKeydown($event)"
        >
          <span
            aria-hidden="true"
            class="h-12 w-1 rounded-full bg-gray-300 transition-colors group-hover:bg-primary-500 group-focus-visible:bg-primary-500 dark:bg-gray-600 dark:group-hover:bg-primary-400"
          ></span>
        </div>

        <header
          class="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700"
        >
          <div class="min-w-0 flex-1">
            <h2
              class="truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            >
              {{ ref.filename }}
            </h2>
            <p class="text-xs text-gray-500 dark:text-gray-400">
              {{ kindLabel() }}
            </p>
          </div>

          <a
            class="flex size-8 items-center justify-center rounded-md text-gray-500 no-underline! transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            [href]="downloadHref()"
            [attr.download]="ref.filename"
            [attr.aria-label]="'Download ' + ref.filename"
            [appTooltip]="'Download'"
            appTooltipPosition="bottom"
            rel="noopener noreferrer"
          >
            <ng-icon
              name="heroArrowDownTray"
              class="text-lg"
              aria-hidden="true"
            />
          </a>

          <button
            type="button"
            class="flex size-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            aria-label="Close document preview"
            (click)="close()"
          >
            <ng-icon name="heroXMark" class="text-lg" aria-hidden="true" />
          </button>
        </header>

        <div class="relative min-h-0 flex-1" [class.pointer-events-none]="dragging()">
          @if (error(); as message) {
            <div
              class="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center"
              role="alert"
            >
              <ng-icon
                name="heroExclamationTriangle"
                class="text-3xl text-state-warning-500"
                aria-hidden="true"
              />
              <p class="text-sm text-gray-700 dark:text-gray-300">
                {{ message }}
              </p>
              @if (retryable()) {
                <button
                  type="button"
                  class="rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm font-medium text-white transition-[filter] hover:brightness-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-900"
                  (click)="retry()"
                >
                  Try again
                </button>
              }
            </div>
          } @else {
            @switch (kind()) {
              @case ('pptx') {
                <app-pptx-viewer
                  [bytes]="bytes()"
                  (renderFailed)="onRenderFailed($event)"
                  (rendered)="onRendered()"
                />
              }
              @case ('csv') {
                <app-csv-viewer
                  [bytes]="bytes()"
                  (renderFailed)="onRenderFailed($event)"
                  (rendered)="onRendered()"
                />
              }
              @case ('xlsx') {
                <!--
                  Takes the id, not bytes: this viewer asks app-api for
                  rows rather than reading a workbook the browser cannot
                  parse. The panel's own load() never ran for this kind,
                  so bytes() is null by design.
                -->
                <app-xlsx-viewer
                  [uploadId]="ref.uploadId"
                  (renderFailed)="onRenderFailed($event)"
                  (rendered)="onRendered()"
                />
              }
              @default {
                <app-docx-viewer
                  [bytes]="bytes()"
                  (renderFailed)="onRenderFailed($event)"
                  (rendered)="onRendered()"
                />
              }
            }
            @if (!ready()) {
              <div
                class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gray-100 dark:bg-gray-950"
                aria-live="polite"
              >
                <ng-icon
                  name="heroArrowPath"
                  class="animate-spin text-2xl text-gray-400"
                  aria-hidden="true"
                />
                <p class="text-sm text-gray-500 dark:text-gray-400">
                  Loading preview…
                </p>
              </div>
            }
          }
        </div>
      </aside>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class FilePreviewPanelComponent {
  private readonly previewState = inject(FilePreviewStateService);
  private readonly previewHttp = inject(FilePreviewHttpService);
  private readonly dockedPane = inject(DockedPaneService);
  private readonly config = inject(ConfigService);

  protected readonly open = this.previewState.openFile;

  protected readonly bytes = signal<ArrayBuffer | null>(null);
  /** Which viewer renders the current file. Derived from the filename so
   *  the header reads correctly while the fetch is still in flight, then
   *  confirmed against the server's MIME type in
   *  `FilePreviewHttpService.fetchDocument` before any bytes are shown. */
  protected readonly kind = computed<PreviewKind>(() => {
    const ref = this.open();
    return (ref && previewKindFor(ref.filename)) || 'docx';
  });
  protected readonly kindLabel = computed(
    () => PREVIEW_KIND_LABELS[this.kind()],
  );
  protected readonly error = signal<string | null>(null);
  protected readonly retryable = signal(false);
  /** Cleared only once the renderer reports a painted document, so the
   *  skeleton covers the parse too — a large `.docx` spends far longer
   *  in `renderAsync` than it does in flight. */
  protected readonly ready = signal(false);

  protected readonly downloadHref = computed(() => {
    const ref = this.open();
    return ref ? downloadUrlFor(this.config.appApiUrl(), ref.uploadId) : '';
  });

  // Rail width — shared with the artifact pane so resizing one resizes
  // "the rail", which is what a user who moved the boundary expects.
  protected readonly paneWidth = this.dockedPane.width;
  protected readonly paneWidthMin = this.dockedPane.widthMin;
  protected readonly paneWidthMax = this.dockedPane.widthMax;
  protected readonly paneWidthCss = computed(
    () => `${this.dockedPane.width()}px`,
  );
  protected readonly dragging = signal(false);
  private dragStartX = 0;
  private dragStartWidth = 0;

  /** Bumped per load so a slow fetch that resolves after the pane closed
   *  or switched file is discarded. */
  private requestSeq = 0;

  constructor() {
    effect(() => {
      const ref = this.open();
      this.requestSeq++;
      this.reset();
      if (!ref) return;

      // An .xlsx is read by app-api, so there is nothing to fetch here
      // — running the presigned-URL leg anyway would pull the whole
      // workbook into the browser only to ignore it. Its viewer owns
      // both the request and the failure, and reports them through the
      // same (rendered)/(renderFailed) pair as every other viewer.
      const kind = previewKindFor(ref.filename);
      if (kind && !previewFetchesBytes(kind)) return;

      void this.load(ref.uploadId);
    });
  }

  private reset(): void {
    this.bytes.set(null);
    this.error.set(null);
    this.retryable.set(false);
    this.ready.set(false);
  }

  private async load(uploadId: string): Promise<void> {
    const seq = ++this.requestSeq;
    this.reset();

    try {
      const doc = await this.previewHttp.fetchDocument(uploadId);
      if (seq !== this.requestSeq) return;
      this.bytes.set(doc.bytes);
    } catch (e) {
      if (seq !== this.requestSeq) return;
      const failure =
        e instanceof FilePreviewError
          ? e
          : new FilePreviewError('Something went wrong loading this file.', true);
      this.error.set(failure.message);
      this.retryable.set(failure.retryable);
    }
  }

  protected retry(): void {
    const ref = this.open();
    if (!ref) return;

    const kind = previewKindFor(ref.filename);
    if (kind && !previewFetchesBytes(kind)) {
      // Nothing to refetch here — clearing the error re-creates the
      // viewer, whose own effect issues the request again.
      this.reset();
      return;
    }

    void this.load(ref.uploadId);
  }

  protected onRendered(): void {
    this.ready.set(true);
  }

  protected onRenderFailed(message: string): void {
    // A parse failure is not retryable: the same bytes will fail the
    // same way. Download stays in the header, which is the useful
    // remaining action.
    this.error.set(message);
    this.retryable.set(false);
    this.ready.set(true);
  }

  protected close(): void {
    this.previewState.close();
  }

  protected onEscape(): void {
    if (this.open()) this.close();
  }

  protected onHandlePointerDown(e: PointerEvent): void {
    e.preventDefault();
    try {
      (e.target as Element).setPointerCapture(e.pointerId);
    } catch {
      /* not all pointer types/environments allow capture — drag still works */
    }
    this.dragStartX = e.clientX;
    this.dragStartWidth = this.dockedPane.width();
    this.dragging.set(true);
  }

  protected onHandlePointerMove(e: PointerEvent): void {
    if (!this.dragging()) return;
    // Pane is docked to the right edge: dragging left (a smaller
    // clientX) widens it.
    this.applyWidth(this.dragStartWidth + (this.dragStartX - e.clientX));
  }

  protected onHandlePointerUp(e: PointerEvent): void {
    if (!this.dragging()) return;
    this.dragging.set(false);
    try {
      const el = e.target as Element;
      if (el.hasPointerCapture(e.pointerId)) {
        el.releasePointerCapture(e.pointerId);
      }
    } catch {
      /* capture may never have been established — nothing to release */
    }
  }

  protected onHandleKeydown(e: KeyboardEvent): void {
    const step = e.shiftKey ? 64 : 16;
    const w = this.dockedPane.width();
    switch (e.key) {
      case 'ArrowLeft': // widen (boundary moves left)
        this.applyWidth(w + step);
        break;
      case 'ArrowRight': // narrow
        this.applyWidth(w - step);
        break;
      case 'Home':
        this.applyWidth(this.paneWidthMax);
        break;
      case 'End':
        this.applyWidth(this.paneWidthMin);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  /** Clamp to the service's absolute bounds and to a viewport-relative
   *  ceiling so the chat column can never be squeezed away entirely.
   *  The 360px floor for the chat matches `ArtifactPanelComponent` — one
   *  rail, so one answer to how narrow the conversation may get. */
  private applyWidth(px: number): void {
    let target = px;
    if (typeof window !== 'undefined') {
      const maxByViewport = Math.max(
        this.paneWidthMin,
        window.innerWidth - 360,
      );
      target = Math.min(target, maxByViewport);
    }
    this.dockedPane.setWidth(target);
  }
}
