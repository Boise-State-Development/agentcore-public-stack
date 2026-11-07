import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';
import { KNOWLEDGE_MODE_UNKNOWN, KnowledgeModeService, KnowledgeModeStatus } from './knowledge-mode.service';

/** Thousands with a separator, rounded to the nearest thousand past 10K ("~48K"). */
function formatTokens(tokens: number): string {
  if (tokens >= 10_000) {
    return `${Math.round(tokens / 1000)}K`;
  }
  return tokens.toLocaleString('en-US');
}

/** The sentence after "Searched:" that says why, or null. */
function searchedBecause(status: KnowledgeModeStatus): string | null {
  switch (status.reason) {
    case 'over_budget':
      return `its text (~${formatTokens(status.estimatedTokens)} tokens) is over the ${formatTokens(status.budgetTokens)}-token limit for sending it whole.`;
    case 'raw_bytes':
      return 'its documents are too large to send whole.';
    case 'document_over_budget':
      return `one document is about ${formatTokens(status.estimatedTokens)} tokens on its own, over the ${formatTokens(status.budgetTokens)}-token limit for sending it whole.`;
    case 'no_text_layer':
      return 'a document has no readable text (a scan, for example).';
    case 'no_text_documents':
      return 'it holds only spreadsheets or presentations, which are always searched.';
    case 'full_context_off':
      return 'this deployment does not send knowledge bases whole.';
    default:
      return null;
  }
}

/**
 * One read-only line on the Agent Designer's knowledge-base card: is the
 * knowledge base sent to the model whole, or searched per message, and why.
 *
 * An author who writes "read Process.txt in full" in the instructions needs to
 * know which of the two the model gets. Re-read whenever `refreshKey` changes,
 * which the parent bumps when its document list changes. Renders nothing while
 * loading, for an empty knowledge base, or when the mode can't be read.
 */
@Component({
  selector: 'app-knowledge-mode-line',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (line(); as text) {
      <p class="text-sm/6 text-gray-600 dark:text-gray-300" data-testid="knowledge-mode-line">
        <span class="font-medium text-gray-900 dark:text-white">{{ text.lead }}</span>
        {{ text.detail }}
      </p>
    }
  `,
})
export class KnowledgeModeLineComponent {
  private readonly service = inject(KnowledgeModeService);

  readonly entityId = input<string | null>(null);
  /** Any value that changes when the knowledge base's documents do. */
  readonly refreshKey = input<unknown>(null);

  readonly status = signal<KnowledgeModeStatus>(KNOWLEDGE_MODE_UNKNOWN);
  private requestSeq = 0;

  readonly line = computed<{ lead: string; detail: string } | null>(() => {
    const status = this.status();
    if (status.mode === 'whole') {
      const count = status.documentCount;
      const documents = `${count} document${count === 1 ? '' : 's'}`;
      const skipped = status.skippedDocuments.length;
      const note = skipped
        ? ` Spreadsheets and presentations (${skipped}) are searched instead.`
        : '';
      return {
        lead: 'Sent to the model whole',
        detail: `(${documents}, ~${formatTokens(status.estimatedTokens)} tokens).${note}`,
      };
    }
    if (status.mode === 'searched') {
      const because = searchedBecause(status);
      return {
        lead: 'Searched:',
        detail: `the model sees the passages that match each message${because ? `, because ${because}` : '.'}`,
      };
    }
    return null;
  });

  constructor() {
    effect(() => {
      const id = this.entityId();
      this.refreshKey();
      void this.load(id);
    });
  }

  private async load(id: string | null): Promise<void> {
    const seq = ++this.requestSeq;
    if (!id) {
      this.status.set(KNOWLEDGE_MODE_UNKNOWN);
      return;
    }
    const status = await this.service.getMode(id);
    // A newer request (another document landed) wins over a slower older one.
    if (seq === this.requestSeq) {
      this.status.set(status);
    }
  }
}
