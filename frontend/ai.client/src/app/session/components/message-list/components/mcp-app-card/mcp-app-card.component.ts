import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroExclamationTriangle,
  heroPuzzlePiece,
} from '@ng-icons/heroicons/outline';
import type { McpAppCard } from '../../../../services/mcp-apps/mcp-app-card-state.service';

/**
 * Static historical card for an app-initiated tool call (MCP Apps PR #6,
 * Option A). Rendered on reload from persisted provenance — read-only by
 * design: the App iframe itself is not re-instantiated, so this records
 * *what the embedded app ran on the user's behalf*, not an interactive
 * surface. Live app-initiated calls render through the normal tool path
 * (PR #5); this only appears after a refresh.
 */
@Component({
  selector: 'app-mcp-app-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroExclamationTriangle, heroPuzzlePiece }),
  ],
  host: { class: 'block' },
  template: `
    <div
      class="flex max-w-xl flex-col gap-1.5 rounded-lg border border-gray-200/80 bg-white px-3 py-2 text-sm dark:border-white/10 dark:bg-slate-800/70"
      role="group"
      [attr.aria-label]="'App ran ' + card().toolName"
    >
      <div class="flex items-center gap-2">
        <ng-icon
          name="heroPuzzlePiece"
          class="size-4 shrink-0 text-primary-600 dark:text-primary-300"
          aria-hidden="true"
        />
        <span
          class="text-[10px] font-semibold uppercase tracking-[0.08em] text-primary-600 dark:text-primary-300"
        >
          Ran by app
        </span>
        <span
          class="truncate font-mono text-xs text-gray-900 dark:text-gray-100"
        >
          {{ card().toolName }}
        </span>
        @if (card().isError) {
          <ng-icon
            name="heroExclamationTriangle"
            class="size-4 shrink-0 text-red-600 dark:text-red-400"
            [attr.aria-label]="'errored'"
          />
        }
      </div>

      @if (argsPreview(); as args) {
        <pre
          class="overflow-x-auto rounded bg-gray-50 px-2 py-1 text-[11px] leading-relaxed text-gray-700 dark:bg-slate-900 dark:text-gray-300"
        >{{ args }}</pre>
      }

      @if (resultText(); as text) {
        <p
          class="whitespace-pre-wrap text-xs/5 text-gray-700 dark:text-gray-300"
        >
          {{ text }}
        </p>
      }
    </div>
  `,
})
export class McpAppCardComponent {
  readonly card = input.required<McpAppCard>();

  protected readonly argsPreview = computed<string | null>(() => {
    const args = this.card().arguments;
    if (!args || Object.keys(args).length === 0) return null;
    let text: string;
    try {
      text = JSON.stringify(args);
    } catch {
      return null;
    }
    return text.length > 300 ? `${text.slice(0, 300)}…` : text;
  });

  protected readonly resultText = computed<string | null>(() => {
    const blocks = this.card().content ?? [];
    const parts: string[] = [];
    for (const block of blocks) {
      const text = (block as { text?: unknown }).text;
      if (typeof text === 'string' && text) parts.push(text);
    }
    const joined = parts.join('\n').trim();
    if (!joined) return null;
    return joined.length > 500 ? `${joined.slice(0, 500)}…` : joined;
  });
}
