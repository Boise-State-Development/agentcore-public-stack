import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from '@angular/core';
import { ChatStateService } from '../../services/chat/chat-state.service';

/**
 * Compact badge above the chat composer showing the running USD cost of
 * this conversation and the % of the model's context window used by the
 * most recent turn. Reads directly from ChatStateService — seeded on
 * session metadata load and incrementally updated by the stream parser
 * after each turn.
 */
@Component({
  selector: 'app-session-cost-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (visible()) {
      <div
        class="inline-flex items-center gap-2 text-xs leading-none text-gray-500 dark:text-gray-400"
        role="status"
        aria-live="polite"
      >
        <span [attr.aria-label]="'Session cost: ' + costLabel()">{{ costLabel() }}</span>
        @if (showContext()) {
          <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
          <span
            [class]="contextClass()"
            [title]="contextTooltip()"
            [attr.aria-label]="contextAriaLabel()"
          >
            {{ contextLabel() }} context
          </span>
        }
      </div>
    }
  `,
})
export class SessionCostBadgeComponent {
  private chatStateService = inject(ChatStateService);

  protected readonly cost = this.chatStateService.costDollars;
  protected readonly contextTokens = this.chatStateService.contextTokens;
  protected readonly contextWindow = this.chatStateService.contextWindowSize;
  protected readonly contextPctValue = this.chatStateService.contextPct;

  protected readonly visible = computed(
    () => this.cost() > 0 || this.contextWindow() > 0,
  );

  protected readonly showContext = computed(() => this.contextWindow() > 0);

  protected readonly costLabel = computed(() => {
    const value = this.cost();
    if (value <= 0) return '$0.00';
    if (value < 0.01) return '<$0.01';
    if (value < 1) return `$${value.toFixed(4)}`;
    return `$${value.toFixed(2)}`;
  });

  protected readonly contextLabel = computed(() => {
    const pct = this.contextPctValue();
    if (pct <= 0) return '0%';
    if (pct < 1) return '<1%';
    return `${Math.round(pct)}%`;
  });

  protected readonly contextClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'text-red-600 dark:text-red-400 font-medium';
    if (pct >= 70) return 'text-amber-600 dark:text-amber-400 font-medium';
    return '';
  });

  protected readonly contextTooltip = computed(() => {
    const tokens = this.contextTokens();
    const window = this.contextWindow();
    if (!window) return '';
    return `${tokens.toLocaleString()} of ${window.toLocaleString()} tokens used (includes system prompt and tools)`;
  });

  protected readonly contextAriaLabel = computed(
    () => `Context window: ${this.contextLabel()} used`,
  );
}
