import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowDownTray, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../components/dialog/dialog-shell.component';
import { SpinnerComponent } from '../components/spinner/spinner.component';
import { DocumentService } from '../assistants/services/document.service';

export interface DocumentViewerDialogData {
  /** The agent the document belongs to. */
  target: string;
  documentId: string;
  filename: string;
  /** Markdown renders; text shows as it is. */
  format: 'markdown' | 'text';
}

/** Above this the viewer offers the file instead: a preview is for reading, not for a data dump. */
export const DOCUMENT_VIEWER_MAX_BYTES = 2 * 1024 * 1024;

/**
 * Read a text document from an agent's knowledge base as it was uploaded:
 * markdown rendered, plain text, CSV or SQL as is. Fetches the stored file
 * through its short-lived download URL (the knowledge bucket allows GET from the
 * app's origins), so nothing new is stored or served.
 *
 * This is the original, which is what an author checking a source wants. What
 * the knowledge base *extracted* from it is a different question, and stays in
 * "View extracted content".
 *
 * Markdown goes through ngx-markdown's default sanitising, like every other
 * markdown the app renders: a document is someone's upload, never trusted HTML.
 */
@Component({
  selector: 'app-document-viewer-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, MarkdownComponent, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroArrowDownTray, heroExclamationTriangle })],
  template: `
    <app-dialog-shell [title]="data.filename" size="xl" (closed)="close()">
      @if (loading()) {
        <div class="flex items-center justify-center py-12">
          <app-spinner size="md" label="Loading the document" />
        </div>
      } @else if (error(); as message) {
        <div role="alert" class="flex items-start gap-3 rounded-xl bg-state-warning-50 px-4 py-3 text-sm/6 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-300">
          <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-5 shrink-0" aria-hidden="true" />
          <div class="min-w-0">
            <p>{{ message }}</p>
            @if (downloadUrl()) {
              <a
                [href]="downloadUrl()"
                target="_blank"
                rel="noopener noreferrer"
                class="mt-2 inline-flex items-center gap-1.5 font-medium underline underline-offset-2"
              >
                <ng-icon name="heroArrowDownTray" class="size-4" aria-hidden="true" />
                Download it instead
              </a>
            }
          </div>
        </div>
      } @else if (data.format === 'markdown') {
        <div class="message-block overflow-x-auto text-sm/6 text-gray-800 dark:text-gray-200">
          <markdown [data]="content()" />
        </div>
      } @else {
        <pre class="overflow-x-auto rounded-xl bg-gray-50 p-4 font-mono text-xs/5 whitespace-pre-wrap text-gray-800 dark:bg-gray-900 dark:text-gray-200">{{ content() }}</pre>
      }
    </app-dialog-shell>
  `,
})
export class DocumentViewerDialogComponent {
  protected readonly data = inject<DocumentViewerDialogData>(DIALOG_DATA);
  private readonly dialogRef = inject<DialogRef<void>>(DialogRef);
  private readonly documents = inject(DocumentService);

  protected readonly loading = signal(true);
  protected readonly content = signal('');
  protected readonly error = signal<string | null>(null);
  protected readonly downloadUrl = signal<string | null>(null);

  constructor() {
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const { downloadUrl } = await this.documents.getDownloadUrl(this.data.target, this.data.documentId);
      this.downloadUrl.set(downloadUrl);
      const response = await fetch(downloadUrl);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const size = Number(response.headers.get('Content-Length') ?? '0');
      if (size > DOCUMENT_VIEWER_MAX_BYTES) {
        this.error.set('This document is too large to preview here.');
        return;
      }
      const text = await response.text();
      if (text.length > DOCUMENT_VIEWER_MAX_BYTES) {
        this.error.set('This document is too large to preview here.');
        return;
      }
      this.content.set(text);
    } catch {
      this.error.set('This document could not be loaded.');
    } finally {
      this.loading.set(false);
    }
  }

  protected close(): void {
    this.dialogRef.close();
  }
}
