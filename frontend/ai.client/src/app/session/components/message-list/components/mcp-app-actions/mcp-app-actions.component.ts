import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import type { McpAppCard } from '../../../../services/mcp-apps/mcp-app-card-state.service';

/**
 * Provenance detail for the tool calls an embedded MCP App ran on the
 * user's behalf (MCP Apps PR #6, Option A — persisted by `card_store`).
 *
 * Presentational only: the disclosure that reveals it lives in the App
 * frame's header (`McpAppFrameComponent`), which is where an App's actions
 * belong — they're the App's doing, not the model's. The message list uses
 * this same component for the orphan case where no frame can render.
 *
 * Successful runs deliberately collapse into ONE summary line
 * (`board_snapshot ×2, update_task`) rather than a card apiece: an
 * interactive App snapshots and mutates on nearly every user gesture, so a
 * card-per-call turns a working board into a wall of history. Failures are
 * the exception — they're listed individually, with their error text,
 * because "what did this app do that didn't work" is the question the
 * record exists to answer.
 */
@Component({
  selector: 'app-mcp-app-actions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <div class="flex flex-col gap-1.5 text-xs">
      @if (successSummary(); as summary) {
        <div class="flex items-baseline gap-1.5">
          <span class="shrink-0 text-gray-500 dark:text-gray-400">
            {{ succeeded().length }} succeeded
          </span>
          <span
            class="min-w-0 truncate font-mono text-gray-700 dark:text-gray-300"
            [title]="summary"
            >{{ summary }}</span
          >
        </div>
      }

      @for (failure of failures(); track failure.card.cardId) {
        <div class="flex items-start gap-1.5">
          <ng-icon
            name="heroExclamationTriangle"
            class="mt-px size-3.5 shrink-0 text-state-danger-600 dark:text-state-danger-400"
            aria-hidden="true"
          />
          <div class="min-w-0">
            <span class="font-mono text-gray-700 dark:text-gray-300">{{
              failure.card.toolName
            }}</span>
            <span class="ml-1.5 text-state-danger-600 dark:text-state-danger-400"
              >failed</span
            >
            @if (failure.message) {
              <p class="whitespace-pre-wrap text-gray-600 dark:text-gray-400">
                {{ failure.message }}
              </p>
            }
          </div>
        </div>
      }
    </div>
  `,
})
export class McpAppActionsComponent {
  readonly cards = input.required<readonly McpAppCard[]>();

  protected readonly succeeded = computed(() =>
    this.cards().filter((card) => !card.isError),
  );

  /**
   * Successful runs as `toolName ×N`, in first-seen order (cards arrive
   * oldest-first, so the order is stable across renders). Null when
   * nothing succeeded, so the template omits the line entirely.
   */
  protected readonly successSummary = computed<string | null>(() => {
    const counts = new Map<string, number>();
    for (const card of this.succeeded()) {
      counts.set(card.toolName, (counts.get(card.toolName) ?? 0) + 1);
    }
    if (!counts.size) return null;
    return [...counts.entries()]
      .map(([name, count]) => (count > 1 ? `${name} ×${count}` : name))
      .join(', ');
  });

  protected readonly failures = computed(() =>
    this.cards()
      .filter((card) => card.isError)
      .map((card) => ({ card, message: resultText(card) })),
  );
}

/** First 200 chars of the card's text result blocks, or null. */
function resultText(card: McpAppCard): string | null {
  const parts: string[] = [];
  for (const block of card.content ?? []) {
    const text = (block as { text?: unknown }).text;
    if (typeof text === 'string' && text) parts.push(text);
  }
  const joined = parts.join('\n').trim();
  if (!joined) return null;
  return joined.length > 200 ? `${joined.slice(0, 200)}…` : joined;
}
