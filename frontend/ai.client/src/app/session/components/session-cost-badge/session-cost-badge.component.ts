import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  Injector,
  input,
  signal,
  untracked,
} from '@angular/core';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';

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

// The cost counts up to the new conversation total whenever the total
// changes — on entrance (in step with the label's own fade-in) and again
// after every turn, so a turn's spend reads as movement rather than as a
// number that silently swapped. Counting only ever goes *up*: a drop means
// a different session is now in view, and tallying downward towards another
// conversation's total would be animating a number that never happened.
const COST_COUNT_UP_MS = 650;

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
          class="badge-cost-enter group/quota relative inline-flex items-center rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
          [attr.tabindex]="hasQuotaTooltip() ? 0 : null"
          [attr.aria-label]="costAriaLabel()"
        >
          <!--
            The digits are aria-hidden so the count-up's ~40 intermediate
            frames never reach the surrounding polite live region; the span's
            own aria-label already carries the settled figure, so a screen
            reader hears the turn's total once instead of watching it tick.
          -->
          <span class="tabular-nums" aria-hidden="true">{{ displayedCostLabel() }}</span>

          @if (hasQuotaTooltip()) {
            <span
              role="tooltip"
              class="pointer-events-none absolute bottom-full -left-1 z-10 mb-2 w-60 rounded-md border border-gray-200 bg-white p-3 text-left shadow-lg opacity-0 transition-opacity duration-150 group-hover/quota:opacity-100 group-focus-within/quota:opacity-100 dark:border-gray-700 dark:bg-gray-800"
            >
              <span class="block text-[11px] font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Quota usage
              </span>

              @if (quotaInfo(); as q) {
                <span class="mt-1 flex items-baseline gap-1.5">
                  <span [class]="quotaPctClass()" class="text-lg font-semibold leading-none">{{ q.pctLabel }}</span>
                  <span class="text-[11px] leading-none text-gray-500 dark:text-gray-400">used this {{ q.periodWord }}</span>
                </span>
                <span class="mt-2 block text-xs text-gray-700 dark:text-gray-300">
                  {{ q.usageLabel }}
                  <span class="text-gray-500 dark:text-gray-400">of</span>
                  {{ q.limitLabel }}
                </span>
                <span class="mt-1 block text-[11px] leading-snug text-gray-500 dark:text-gray-400">
                  {{ q.remainingLabel }} remaining@if (q.resetInfo) { · {{ q.resetInfo }} }
                </span>
              } @else {
                <span class="mt-1 block text-xs text-gray-700 dark:text-gray-300">
                  Unlimited quota — no spending limit applies to your account.
                </span>
              }
            </span>
          }
        </span>

        @if (showContext()) {
          <span
            class="badge-ring-enter text-gray-300 dark:text-gray-600"
            aria-hidden="true"
          >·</span>

          <span
            class="badge-ring-enter group relative inline-flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-1 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-900"
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
              class="pointer-events-none absolute bottom-full -left-1 z-10 mb-2 w-56 rounded-md border border-gray-200 bg-white p-3 text-left shadow-lg opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 dark:border-gray-700 dark:bg-gray-800"
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
  private quotaStatusService = inject(QuotaStatusService);
  private injector = inject(Injector);

  /**
   * Which session to report on. Omit it — as the main chat does — and the badge
   * follows the *viewed* session, which is the right behaviour for the composer
   * the user is typing into.
   *
   * Pass it for a chat that is on screen without being the viewed session: the
   * Designer's preview and the marketplace test drive stream into their own
   * `preview-` sessions and deliberately never call `setViewedSession`, so an
   * unpinned badge there would report the cost of whatever conversation the
   * user last opened — a plausible-looking number belonging to a different
   * conversation, which is worse than no badge at all.
   */
  readonly sessionId = input<string | null>(null);

  protected readonly cost = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.costDollarsFor(id) : this.chatStateService.costDollars();
  });

  protected readonly contextTokens = computed(() => {
    const id = this.sessionId();
    return id
      ? this.chatStateService.contextTokensFor(id)
      : this.chatStateService.contextTokens();
  });

  protected readonly contextWindow = computed(() => {
    const id = this.sessionId();
    return id
      ? this.chatStateService.contextWindowFor(id)
      : this.chatStateService.contextWindowSize();
  });

  protected readonly contextPctValue = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.contextPctFor(id) : this.chatStateService.contextPct();
  });

  protected readonly ringSize = 18;
  protected readonly ringCenter = 9;
  protected readonly ringRadius = RING_RADIUS;
  protected readonly ringCircumference = RING_CIRCUMFERENCE;
  protected readonly ringViewBox = '0 0 18 18';

  protected readonly visible = computed(
    () => this.cost() > 0 || this.contextWindow() > 0,
  );

  protected readonly showContext = computed(() => this.contextWindow() > 0);

  /**
   * Format `value` with the digit count the *target* total warrants, not its
   * own. Mid-count the two differ — a tally climbing towards $1.05 passes
   * through $0.98 — and picking the format per-frame would swap four decimals
   * for two partway up, reflowing the badge as it animates.
   */
  private formatCost(value: number, target: number): string {
    if (target <= 0) return '$0.00';
    if (target < 0.01) return '<$0.01';
    const decimals = target < 1 ? 4 : 2;
    return `$${Math.max(0, value).toFixed(decimals)}`;
  }

  protected readonly costLabel = computed(() => {
    const value = this.cost();
    return this.formatCost(value, value);
  });

  /** What the badge paints this frame — the tally, not the settled total. */
  protected readonly displayedCostLabel = computed(() =>
    this.formatCost(this.displayedCost(), this.cost()),
  );

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

  private readonly displayedCostSignal = signal(0);
  protected readonly displayedCost = this.displayedCostSignal.asReadonly();
  private countUpFrame: number | null = null;
  private countUpTimer: ReturnType<typeof setTimeout> | null = null;
  private costEntranceDone = false;

  // displayedOffset starts at the empty-ring value so the SVG paints
  // empty on first render, then updates one frame later — letting the
  // CSS transition animate the fill on entrance. After the first
  // update, signal changes flow through immediately so SSE updates
  // animate via the same transition.
  private readonly displayedOffsetSignal = signal(RING_CIRCUMFERENCE);
  protected readonly displayedOffset = this.displayedOffsetSignal.asReadonly();
  private firstAnimateScheduled = false;

  constructor() {
    inject(DestroyRef).onDestroy(() => this.cancelCountUp());

    effect(() => {
      const target = this.cost();

      if (!this.visible()) {
        // Hidden badge: snap, so the next mount counts up from empty again.
        this.cancelCountUp();
        this.costEntranceDone = false;
        this.displayedCostSignal.set(0);
        return;
      }

      if (!this.costEntranceDone) {
        this.costEntranceDone = true;
        // Hold at zero until the label's own fade-in has begun, then count
        // up underneath it — the same staggered entrance the ring uses.
        untracked(() => this.scheduleCountUp(target, BADGE_ENTRANCE_DELAY_MS));
        return;
      }

      // `untracked`: scheduling reads the running tally to resume from it, and
      // the tally is what this effect's own animation writes — tracking it
      // would restart the count on every frame it produced.
      untracked(() => this.scheduleCountUp(target, 0));
    });

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

  private cancelCountUp(): void {
    if (this.countUpFrame !== null) {
      cancelAnimationFrame(this.countUpFrame);
      this.countUpFrame = null;
    }
    if (this.countUpTimer !== null) {
      clearTimeout(this.countUpTimer);
      this.countUpTimer = null;
    }
  }

  private prefersReducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  /**
   * Tally up to `target`, optionally after `delayMs`. A retarget mid-count
   * (a multi-call turn bills more than once) resumes from wherever the tally
   * had got to rather than restarting, so the number never jumps backwards.
   */
  private scheduleCountUp(target: number, delayMs: number): void {
    this.cancelCountUp();

    const snap = () => this.displayedCostSignal.set(target);

    // A target at or below the tally means the badge switched conversations,
    // not that money was refunded — snap rather than count down.
    if (
      target <= this.displayedCostSignal() ||
      typeof requestAnimationFrame === 'undefined' ||
      this.prefersReducedMotion()
    ) {
      if (delayMs > 0) {
        this.countUpTimer = setTimeout(() => {
          this.countUpTimer = null;
          snap();
        }, delayMs);
      } else {
        snap();
      }
      return;
    }

    const start = () => {
      const from = this.displayedCostSignal();
      const startedAt = performance.now();
      const step = (now: number) => {
        const t = Math.min(1, (now - startedAt) / COST_COUNT_UP_MS);
        // easeOutCubic: fast off the mark, settling onto the final figure.
        const eased = 1 - Math.pow(1 - t, 3);
        this.displayedCostSignal.set(from + (target - from) * eased);
        if (t < 1) {
          this.countUpFrame = requestAnimationFrame(step);
        } else {
          this.countUpFrame = null;
          snap();
        }
      };
      this.countUpFrame = requestAnimationFrame(step);
    };

    if (delayMs > 0) {
      this.countUpTimer = setTimeout(() => {
        this.countUpTimer = null;
        start();
      }, delayMs);
    } else {
      start();
    }
  }

  protected readonly ringStrokeClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'stroke-state-danger-500 dark:stroke-state-danger-400';
    if (pct >= 70) return 'stroke-state-warning-500 dark:stroke-state-warning-400';
    if (pct >= 50) return 'stroke-state-info-500 dark:stroke-state-info-400';
    return 'stroke-state-success-500 dark:stroke-state-success-400';
  });

  protected readonly contextLabelClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'text-state-danger-600 dark:text-state-danger-400 font-medium';
    if (pct >= 70) return 'text-state-warning-600 dark:text-state-warning-400 font-medium';
    return '';
  });

  protected readonly popoverPctClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'text-state-danger-600 dark:text-state-danger-400';
    if (pct >= 70) return 'text-state-warning-600 dark:text-state-warning-400';
    if (pct >= 50) return 'text-state-info-600 dark:text-state-info-400';
    return 'text-state-success-700 dark:text-state-success-400';
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

  // ==========================================================================
  // Quota tooltip — the user's monthly quota context behind the cost counter.
  // Sourced from GET /costs/quota-status (loaded lazily on first read).
  // ==========================================================================

  private readonly quotaStatus = this.quotaStatusService.status;

  private formatUsd(value: number): string {
    if (value > 0 && value < 1) return `$${value.toFixed(4)}`;
    return `$${value.toFixed(2)}`;
  }

  /** Quota breakdown for the tooltip, or null when there's no real limit. */
  protected readonly quotaInfo = computed(() => {
    const status = this.quotaStatus.value();
    if (!status || !status.configured || status.unlimited) return null;
    const limit = status.monthlyLimit;
    if (!limit || limit <= 0) return null;

    const pct = Math.max(0, status.usagePercentage);
    const remaining = status.remaining ?? Math.max(0, limit - status.currentUsage);
    return {
      pct,
      pctLabel: pct < 1 && pct > 0 ? '<1%' : `${Math.round(pct)}%`,
      periodWord: status.periodType === 'daily' ? 'day' : 'month',
      usageLabel: this.formatUsd(status.currentUsage),
      limitLabel: this.formatUsd(limit),
      remainingLabel: this.formatUsd(remaining),
      resetInfo: status.resetInfo,
    };
  });

  /** True when the user is on an unlimited tier/override (still worth a note). */
  protected readonly quotaUnlimited = computed(() => {
    const status = this.quotaStatus.value();
    return !!status && status.configured && status.unlimited;
  });

  /** Whether the cost counter should carry a quota hover/focus tooltip. */
  protected readonly hasQuotaTooltip = computed(
    () => this.quotaInfo() !== null || this.quotaUnlimited(),
  );

  protected readonly quotaPctClass = computed(() => {
    const pct = this.quotaInfo()?.pct ?? 0;
    if (pct >= 90) return 'text-state-danger-600 dark:text-state-danger-400';
    if (pct >= 75) return 'text-state-warning-600 dark:text-state-warning-400';
    return 'text-state-success-600 dark:text-state-success-400';
  });

  protected readonly costAriaLabel = computed(() => {
    const base = `Session cost: ${this.costLabel()}`;
    const q = this.quotaInfo();
    if (q) {
      return `${base}. Monthly quota: ${q.usageLabel} of ${q.limitLabel} used (${q.pctLabel}), ${q.remainingLabel} remaining.`;
    }
    if (this.quotaUnlimited()) return `${base}. Unlimited quota.`;
    return base;
  });
}
