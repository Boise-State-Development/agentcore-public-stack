import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { ChatStateService } from '../../services/chat/chat-state.service';

const RING_RADIUS = 7;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

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
            class="group relative inline-flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
            tabindex="0"
            [attr.aria-label]="contextAriaLabel()"
          >
            <svg
              [attr.width]="ringSize"
              [attr.height]="ringSize"
              [attr.viewBox]="ringViewBox"
              class="-rotate-90"
              aria-hidden="true"
            >
              <circle
                [attr.cx]="ringCenter"
                [attr.cy]="ringCenter"
                [attr.r]="ringRadius"
                fill="none"
                stroke-width="2"
                class="stroke-gray-200 dark:stroke-gray-700"
              />
              <circle
                [attr.cx]="ringCenter"
                [attr.cy]="ringCenter"
                [attr.r]="ringRadius"
                fill="none"
                stroke-width="2"
                stroke-linecap="round"
                [attr.stroke-dasharray]="ringCircumference"
                [style.stroke-dashoffset.px]="displayedOffset()"
                [class]="ringStrokeClass()"
                style="transition: stroke-dashoffset 500ms ease-out;"
              />
            </svg>
            <span [class]="contextLabelClass()">{{ contextLabel() }}</span>

            <!-- Hover/focus popover -->
            <span
              role="tooltip"
              class="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 w-56 -translate-x-1/2 rounded-md border border-gray-200 bg-white p-3 text-left shadow-lg opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 dark:border-gray-700 dark:bg-gray-800"
            >
              <span class="block text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Context window
              </span>
              <span class="mt-1 flex items-baseline gap-1.5">
                <span [class]="popoverPctClass()" class="text-lg font-semibold leading-none">
                  {{ contextLabel() }}
                </span>
                <span class="text-[11px] leading-none text-gray-500 dark:text-gray-400">used</span>
              </span>
              <span class="mt-2 block text-xs text-gray-700 dark:text-gray-300">
                {{ tokensUsedLabel() }}
                <span class="text-gray-500 dark:text-gray-400">of</span>
                {{ tokensTotalLabel() }} tokens
              </span>
              <span class="mt-2 block text-[11px] leading-snug text-gray-500 dark:text-gray-400">
                Includes system prompt and tool definitions.
              </span>
            </span>
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

  protected readonly ringSize = 18;
  protected readonly ringCenter = 9;
  protected readonly ringRadius = RING_RADIUS;
  protected readonly ringCircumference = RING_CIRCUMFERENCE;
  protected readonly ringViewBox = '0 0 18 18';

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

  protected readonly ringOffset = computed(() => {
    const pct = Math.min(100, Math.max(0, this.contextPctValue()));
    return RING_CIRCUMFERENCE * (1 - pct / 100);
  });

  // displayedOffset starts at the empty-ring value so the SVG paints
  // empty on first render, then updates one frame later — letting the
  // CSS transition animate the fill on entrance. After the first
  // update, signal changes flow through immediately so SSE updates
  // animate via the same transition.
  private readonly displayedOffsetSignal = signal(RING_CIRCUMFERENCE);
  protected readonly displayedOffset = this.displayedOffsetSignal.asReadonly();
  private firstAnimateScheduled = false;

  constructor() {
    effect(() => {
      // Reset to empty when the ring is hidden so the next mount animates again.
      if (!this.showContext()) {
        this.displayedOffsetSignal.set(RING_CIRCUMFERENCE);
        this.firstAnimateScheduled = false;
        return;
      }

      const target = this.ringOffset();
      if (!this.firstAnimateScheduled) {
        this.firstAnimateScheduled = true;
        // Yield the main thread for one paint cycle so the SVG commits
        // its empty state to the screen, *then* update the offset so
        // the CSS transition animates from empty → target. RAF alone
        // is unreliable here because Angular's signal-driven CD can
        // coalesce both DOM mutations into a single paint frame.
        setTimeout(() => this.displayedOffsetSignal.set(target), 80);
      } else {
        this.displayedOffsetSignal.set(target);
      }
    });
  }

  protected readonly ringStrokeClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'stroke-red-500 dark:stroke-red-400';
    if (pct >= 70) return 'stroke-amber-500 dark:stroke-amber-400';
    if (pct >= 50) return 'stroke-blue-500 dark:stroke-blue-400';
    return 'stroke-emerald-500 dark:stroke-emerald-400';
  });

  protected readonly contextLabelClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'text-red-600 dark:text-red-400 font-medium';
    if (pct >= 70) return 'text-amber-600 dark:text-amber-400 font-medium';
    return '';
  });

  protected readonly popoverPctClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'text-red-600 dark:text-red-400';
    if (pct >= 70) return 'text-amber-600 dark:text-amber-400';
    if (pct >= 50) return 'text-blue-600 dark:text-blue-400';
    return 'text-emerald-600 dark:text-emerald-400';
  });

  protected readonly tokensUsedLabel = computed(() =>
    this.contextTokens().toLocaleString(),
  );

  protected readonly tokensTotalLabel = computed(() =>
    this.contextWindow().toLocaleString(),
  );

  protected readonly contextAriaLabel = computed(() => {
    const tokens = this.contextTokens().toLocaleString();
    const window = this.contextWindow().toLocaleString();
    return `Context window: ${this.contextLabel()} used (${tokens} of ${window} tokens, includes system prompt and tools)`;
  });
}
