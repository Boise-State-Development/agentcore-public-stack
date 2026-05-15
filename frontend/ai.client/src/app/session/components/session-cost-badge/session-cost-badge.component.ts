import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  Injector,
  signal,
} from '@angular/core';
import { ChatStateService } from '../../services/chat/chat-state.service';

const RING_RADIUS = 7;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

// Staggered entrance, offset from the chat-container footer's own
// 300ms `animate-fade-in` so the badge reads as a separate event
// rather than animating in alongside the input footer.
//   0   – 300ms  : (chat input footer fades in — not us)
//   350 – 600ms  : cost label fades + slides up
//   500 – 750ms  : separator + ring container fade in (overlaps cost tail)
//   750 – 1250ms : ring dash-offset fills from empty → target
const BADGE_ENTRANCE_DELAY_MS = 350;
const COST_ENTRANCE_MS = 250;
const RING_ENTRANCE_DELAY_MS = BADGE_ENTRANCE_DELAY_MS + 150;
const RING_FILL_DELAY_MS = 750;

@Component({
  selector: 'app-session-cost-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    @keyframes badgeFadeInUp {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    .badge-cost-enter {
      animation: badgeFadeInUp ${COST_ENTRANCE_MS}ms ease-out ${BADGE_ENTRANCE_DELAY_MS}ms backwards;
    }
    .badge-ring-enter {
      animation: badgeFadeInUp ${COST_ENTRANCE_MS}ms ease-out ${RING_ENTRANCE_DELAY_MS}ms backwards;
    }
  `,
  template: `
    @if (visible()) {
      <div
        class="inline-flex items-center gap-2 text-xs leading-none text-gray-500 dark:text-gray-400"
        role="status"
        aria-live="polite"
      >
        <span
          class="badge-cost-enter"
          [attr.aria-label]="'Session cost: ' + costLabel()"
        >{{ costLabel() }}</span>

        @if (showContext()) {
          <span
            class="badge-ring-enter text-gray-300 dark:text-gray-600"
            aria-hidden="true"
          >·</span>

          <span
            class="badge-ring-enter group relative inline-flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
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
                Reflects the most recent turn (includes system prompt and tool definitions). May shrink after a context compaction.
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
  private injector = inject(Injector);

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
        // Staggered entrance: the cost label and ring container have
        // CSS fade-in-up animations that finish around RING_FILL_DELAY_MS.
        // Wait for Angular to commit the SVG (afterNextRender), then
        // delay the dash-offset update until the entrance animations
        // have settled before kicking off the ring fill.
        afterNextRender(
          () => {
            requestAnimationFrame(() => {
              setTimeout(
                () => this.displayedOffsetSignal.set(target),
                RING_FILL_DELAY_MS,
              );
            });
          },
          { injector: this.injector },
        );
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
    return `Context window: ${this.contextLabel()} used by the most recent turn (${tokens} of ${window} tokens, includes system prompt and tools)`;
  });
}
