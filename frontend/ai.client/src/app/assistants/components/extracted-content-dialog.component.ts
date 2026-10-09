import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { DialogDescriptionDirective } from '../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { DocumentService } from '../services/document.service';
import { ExtractedChunk } from '../models/document.model';

export interface ExtractedContentDialogData {
  assistantId: string;
  documentId: string;
  filename: string;
}

/**
 * Shows the content the knowledge base actually extracted from one document.
 *
 * Why this exists (managed-kb-migration §5.41, task 16.2): the managed backend's
 * vision/parse step flattens a column-structured flowchart or a two-dimensional table
 * at ingestion. A question about "semester 4" then gets a *confident wrong answer* with
 * nothing to indicate why. The decision was not to fix the parser — it is a managed
 * service, and on a self-service platform the mitigation is guidance — but guidance
 * nobody can verify is not guidance. This panel is where an owner sees that their table
 * came out as one long line, and decides to reformat their source.
 *
 * Three deliberate presentation choices:
 *
 * - **Monospace, whitespace preserved.** A flattened table rendered as flowing prose
 *   looks fine. The damage is only visible when the alignment is shown as-is.
 * - **No chunk numbering.** `Retrieve` is query-ranked, so the order here is not the
 *   document's order. Numbering them 1..N would assert a sequence that does not exist.
 * - **The "up to N" note when capped.** Bedrock exposes no chunk-enumeration API, so a
 *   complete set is never guaranteed and claiming one would be a lie the UI tells.
 */
@Component({
  selector: 'app-extracted-content-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDescriptionDirective, DialogShellComponent, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Extracted content" size="xl" (closed)="close()">
      <p appDialogDescription class="mt-1 truncate text-sm/6 text-gray-600 dark:text-gray-400">{{ data.filename }}</p>

      @if (loading()) {
        <div class="flex items-center justify-center py-12">
          <app-spinner />
        </div>
      } @else if (reason()) {
        <!-- Not an error state: the owner asked a fair question and this is the
             answer. A classic knowledge base cannot scope a retrieval to one
             document; a still-processing document has nothing yet. -->
        <div class="flex items-start gap-3 rounded-2xl bg-gray-50 p-4 dark:bg-white/5">
          <ng-icon
            name="heroExclamationTriangle"
            class="size-5 shrink-0 text-gray-400 dark:text-gray-500"
            aria-hidden="true"
          />
          <p class="text-sm/6 text-gray-600 dark:text-gray-300">{{ reason() }}</p>
        </div>
      } @else {
        <!-- The actionable nudge. Without it this panel is a curiosity; with it,
             it tells the owner what to do about what they are looking at. -->
        <p class="text-sm/6 text-gray-600 dark:text-gray-300">
          This is what the knowledge base extracted. The assistant answers from
          <em>this</em>, not from your original layout &mdash; so if a table or an image
          description looks wrong here, consider reformatting your source.
        </p>

        @if (capReached()) {
          <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
            Showing the first {{ returned() }} passages. More may exist &mdash; this
            knowledge base cannot list them all.
          </p>
        } @else {
          <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
            {{ returned() }} {{ returned() === 1 ? 'passage' : 'passages' }}. Order is not
            the document's own order.
          </p>
        }

        <div class="mt-4 space-y-3">
          @for (chunk of chunks(); track chunk.order) {
            <div class="rounded-2xl bg-gray-50 p-3 dark:bg-white/5">
              @if (chunk.page !== null && chunk.page !== undefined) {
                <p class="mb-1.5 text-xs/5 font-medium text-gray-500 dark:text-gray-400">
                  Page {{ chunk.page }}
                </p>
              }
              <!-- Monospace and whitespace-preserved on purpose: a flattened table
                   rendered as prose looks perfectly fine, which is the bug. -->
              <pre
                class="overflow-x-auto font-mono text-xs/5 whitespace-pre-wrap text-gray-800 dark:text-gray-200"
                >{{ chunk.text }}</pre
              >
            </div>
          } @empty {
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">
              The knowledge base returned no content for this document. If it finished
              processing, the file may contain nothing the parser could read &mdash; a
              scanned image with no text layer, for instance.
            </p>
          }
        </div>
      }
    </app-dialog-shell>
  `,
})
export class ExtractedContentDialogComponent {
  readonly data = inject<ExtractedContentDialogData>(DIALOG_DATA);
  private readonly dialogRef = inject<DialogRef<void>>(DialogRef);
  private readonly documents = inject(DocumentService);

  readonly loading = signal(true);
  readonly chunks = signal<ExtractedChunk[]>([]);
  readonly returned = signal(0);
  readonly capReached = signal(false);
  readonly reason = signal<string | null>(null);

  constructor() {
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const result = await this.documents.getExtractedChunks(
        this.data.assistantId,
        this.data.documentId,
      );
      if (!result.available) {
        this.reason.set(result.reason ?? 'This content cannot be shown.');
        return;
      }
      this.chunks.set(result.chunks);
      this.returned.set(result.returned);
      this.capReached.set(result.capReached);
    } catch {
      // Resolve rather than reject into a toast: the panel is already open and an
      // explanation inside it is more useful than an error banner behind it.
      this.reason.set('Could not read the extracted content. Please try again.');
    } finally {
      this.loading.set(false);
    }
  }

  close(): void {
    this.dialogRef.close();
  }
}
