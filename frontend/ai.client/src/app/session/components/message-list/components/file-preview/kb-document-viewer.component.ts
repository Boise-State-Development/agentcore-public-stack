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
import {
  DocumentService,
  DocumentUploadError,
} from '../../../../../assistants/services/document.service';
import { DocumentContentResponse } from '../../../../../assistants/models/document.model';
import { renderIndexedMarkdown } from '../../../../services/file-preview/indexed-markdown';

/** A load failure the pane shows in place, and whether trying again could help. */
export interface KbDocumentLoadFailure {
  message: string;
  retryable: boolean;
}

/**
 * Shows the text a cited knowledge-base document was indexed from — for a
 * crawled web page, exactly what the crawler extracted — rendered from markdown.
 *
 * Like the `.xlsx` viewer it takes ids, not bytes: app-api reads the staged text
 * and returns at most 1 MB of it, so the viewer of a shared agent never gets a
 * URL to the stored object. Markdown goes through `renderIndexedMarkdown` (raw
 * HTML escaped, no remote images) and then Angular's `[innerHTML]` sanitizer.
 *
 * Display only: nothing here is sent to the model.
 */
@Component({
  selector: 'app-kb-document-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (content(); as doc) {
      <div class="h-full overflow-y-auto px-6 py-5" data-testid="kb-document-content">
        @if (doc.truncated) {
          <p
            role="note"
            class="mb-4 rounded-xl bg-gray-100 px-3 py-2 text-xs/5 text-gray-700 dark:bg-gray-800 dark:text-gray-300"
          >
            This document is long, so only its first 1 MB is shown.
          </p>
        }
        @if (!doc.text.trim()) {
          <p class="text-sm/6 text-gray-600 dark:text-gray-400">This document has no indexed text.</p>
        } @else if (doc.contentType === 'text/markdown') {
          <div
            class="message-block overflow-x-auto text-sm/6 text-gray-800 dark:text-gray-200"
            [innerHTML]="html()"
          ></div>
        } @else {
          <pre
            class="font-mono text-xs/5 whitespace-pre-wrap break-words text-gray-800 dark:text-gray-200"
          >{{ doc.text }}</pre>
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }
  `,
})
export class KbDocumentViewerComponent {
  readonly assistantId = input.required<string>();
  readonly documentId = input.required<string>();

  readonly rendered = output<void>();
  readonly loadFailed = output<KbDocumentLoadFailure>();

  private readonly documents = inject(DocumentService);

  protected readonly content = signal<DocumentContentResponse | null>(null);
  protected readonly html = computed(() => {
    const doc = this.content();
    return doc && doc.contentType === 'text/markdown' ? renderIndexedMarkdown(doc.text) : '';
  });

  /** Bumped per load so a slow response for a document the pane has since
   *  moved off is discarded rather than painted. */
  private requestSeq = 0;

  constructor() {
    effect(() => {
      const assistantId = this.assistantId();
      const documentId = this.documentId();
      const seq = ++this.requestSeq;
      this.content.set(null);
      void this.load(assistantId, documentId, seq);
    });
  }

  private async load(assistantId: string, documentId: string, seq: number): Promise<void> {
    try {
      const doc = await this.documents.getDocumentContent(assistantId, documentId);
      if (seq !== this.requestSeq) return;
      this.content.set(doc);
      this.rendered.emit();
    } catch (err) {
      if (seq !== this.requestSeq) return;
      this.loadFailed.emit(describeFailure(err));
    }
  }
}

/** The pane's message for a failed read. The server's own words where it gave
 *  some (type not viewable, viewing turned off, not ready), else a generic one. */
export function describeFailure(err: unknown): KbDocumentLoadFailure {
  const status =
    err instanceof DocumentUploadError && typeof err.details?.['status'] === 'number'
      ? (err.details['status'] as number)
      : 0;
  switch (status) {
    case 404:
      return { message: 'This document is no longer in the knowledge base.', retryable: false };
    case 403:
    case 409:
    case 415:
      return { message: (err as Error).message, retryable: false };
    default:
      return { message: "Couldn't load this document.", retryable: true };
  }
}
