import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
} from '@angular/core';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import { ContextPartition } from '../../services/models/content-types';

const RING_RADIUS = 7;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

// The ring fades in after the composer footer's own 300ms fade, then fills
// from empty, so it reads as its own event rather than part of the footer's.
const RING_ENTRANCE_DELAY_MS = 350;
const RING_FILL_DELAY_MS = 600;

/** Below this share of the window the ring speaks for itself; at or above it
 *  the percentage appears beside the ring, because that is when it matters. */
const SHOW_PCT_AT = 70;

/**
 * Segment colour per partition key. Skills and Memory reuse the section
 * accents the agent form gives those capabilities, so the same thing wears the
 * same colour wherever it appears. An unknown key (a partition the backend
 * adds later) still renders, in a neutral colour.
 */
const PARTITION_COLORS: Record<string, string> = {
  system: 'bg-gray-400 dark:bg-gray-500',
  skills: 'bg-category-accent-skills-500 dark:bg-category-accent-skills-400',
  memory: 'bg-category-accent-memory-500',
  tools: 'bg-tertiary-500 dark:bg-tertiary-400',
  messages: 'bg-secondary-500 dark:bg-secondary-400',
};
const FALLBACK_COLOR = 'bg-state-info-500 dark:bg-state-info-400';

interface MeterSegment {
  key: string;
  label: string;
  tokens: number;
  /** Share of the context window, 0–100, for the stacked bar. */
  pct: number;
  color: string;
  children: { key: string; label: string; tokens: number }[];
}

/** 950 → "950", 12_516 → "12.5k", 1_000_000 → "1M". */
export function formatTokens(value: number): string {
  const n = Math.max(0, Math.round(value));
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${trimZero((n / 1000).toFixed(1))}k`;
  return `${trimZero((n / 1_000_000).toFixed(1))}M`;
}

function trimZero(value: string): string {
  return value.endsWith('.0') ? value.slice(0, -2) : value;
}

function formatUsd(value: number): string {
  if (value <= 0) return '$0.00';
  if (value < 0.01) return '<$0.01';
  return `$${value.toFixed(value < 1 ? 4 : 2)}`;
}

/**
 * The context meter beside the model picker, under a compact composer.
 *
 * At rest it is only a ring — how full the context window was on the most
 * recent turn. Hovering it (or tapping / pressing it) opens a panel with what
 * filled that window — system instructions, skills, memory, tools, messages,
 * free space — and, beneath, what the conversation has cost and where that
 * leaves the user's quota. Cost used to sit on the meta line as a running
 * figure; it lives here now so the line under the composer stays quiet.
 */
@Component({
  selector: 'app-context-meter',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    class: 'relative inline-flex',
    '(pointerenter)': 'onPointerEnter($event)',
    '(pointerleave)': 'onPointerLeave($event)',
    '(document:click)': 'onDocumentClick($event)',
    '(keydown.escape)': 'close()',
  },
  styles: `
    @keyframes meterFadeIn {
      from {
        opacity: 0;
        transform: translateY(4px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    .meter-enter {
      animation: meterFadeIn 250ms ease-out ${RING_ENTRANCE_DELAY_MS}ms backwards;
    }
    .meter-panel {
      animation: meterFadeIn 150ms ease-out;
    }
    @media (prefers-reduced-motion: reduce) {
      .meter-enter,
      .meter-panel {
        animation: none;
      }
    }
  `,
  template: `
      <!-- Always rendered, empty until the first response is measured: it sits
           beside the model picker, and appearing mid-conversation would shift
           the picker sideways. -->
      <button
        type="button"
        class="meter-enter inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs/5 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-primary)] dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-300"
        [attr.aria-label]="triggerAriaLabel()"
        [attr.aria-expanded]="open()"
        aria-controls="context-meter-panel"
        (click)="toggle()"
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
            [attr.visibility]="ringEmpty() ? 'hidden' : null"
            [class]="ringStrokeClass()"
            class="motion-safe:transition-[stroke-dashoffset] motion-safe:duration-500 motion-safe:ease-out"
          />
        </svg>
        @if (showPctInline()) {
          <span class="tabular-nums" [class]="pctTextClass()" aria-hidden="true">{{ contextLabel() }}</span>
        }
      </button>

      @if (open()) {
        <div
          id="context-meter-panel"
          role="region"
          aria-label="Context and cost details"
          class="meter-panel absolute right-0 bottom-full z-20 mb-2 w-[min(20rem,calc(100vw-2rem))] rounded-xl border border-gray-200 bg-white p-3 text-left text-xs/5 text-gray-700 shadow-lg dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
        >
          @if (showContext()) {
            <div class="flex items-baseline justify-between gap-3">
              <span class="font-medium text-gray-900 dark:text-white">Context window</span>
              <span class="tabular-nums text-gray-500 dark:text-gray-400">
                {{ usedLabel() }} / {{ windowLabel() }}
                <span [class]="pctTextClass()">({{ contextLabel() }})</span>
              </span>
            </div>

            <!-- Stacked bar: one segment per partition, the track is free space. -->
            <div
              class="mt-2 flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"
              aria-hidden="true"
            >
              @if (segments().length) {
                @for (s of segments(); track s.key) {
                  <span class="h-full" [class]="s.color" [style.width.%]="s.pct"></span>
                }
              } @else {
                <span class="h-full" [class]="usedOnlyColor()" [style.width.%]="contextPctClamped()"></span>
              }
            </div>

            @if (segments().length) {
              <ul class="mt-2.5 flex flex-col gap-1" aria-label="What is in the context window">
                @for (s of segments(); track s.key) {
                  <li>
                    <div class="flex items-center justify-between gap-3">
                      <span class="flex min-w-0 items-center gap-2">
                        <span class="size-2 shrink-0 rounded-xs" [class]="s.color" aria-hidden="true"></span>
                        <span class="truncate">{{ s.label }}</span>
                      </span>
                      <span class="tabular-nums text-gray-500 dark:text-gray-400">{{ tokenLabel(s.tokens) }}</span>
                    </div>
                    @if (s.children.length > 1) {
                      <ul class="mt-0.5 mb-1 flex flex-col gap-0.5 border-l border-gray-200 pl-3 ml-1 dark:border-gray-700">
                        @for (c of s.children; track c.key) {
                          <li class="flex items-center justify-between gap-3 text-gray-500 dark:text-gray-400">
                            <span class="truncate">{{ c.label }}</span>
                            <span class="tabular-nums">~{{ tokenLabel(c.tokens) }}</span>
                          </li>
                        }
                      </ul>
                    }
                  </li>
                }
                <li class="flex items-center justify-between gap-3">
                  <span class="flex items-center gap-2">
                    <span class="size-2 shrink-0 rounded-xs border border-gray-300 dark:border-gray-600" aria-hidden="true"></span>
                    <span>Free space</span>
                  </span>
                  <span class="tabular-nums text-gray-500 dark:text-gray-400">{{ freeLabel() }}</span>
                </li>
              </ul>
            }

            <p class="mt-2 text-[11px]/4 text-gray-500 dark:text-gray-400">
              @if (segments().length) {
                As of the latest response. Itemized rows are estimates.
              } @else {
                As of the latest response. A breakdown isn't available for this turn.
              }
              Older messages are summarized as the window fills.
            </p>
          } @else {
            <div class="flex items-baseline justify-between gap-3">
              <span class="font-medium text-gray-900 dark:text-white">Context window</span>
              <span class="text-gray-500 dark:text-gray-400">Not measured yet</span>
            </div>
            <div class="mt-2 h-1.5 w-full rounded-full bg-gray-200 dark:bg-gray-700" aria-hidden="true"></div>
            <p class="mt-2 text-[11px]/4 text-gray-500 dark:text-gray-400">
              What fills the window appears here after the next response.
            </p>
          }

          <div class="mt-3 flex flex-col gap-1.5 border-t border-gray-200 pt-3 dark:border-gray-700">
            <div class="flex items-baseline justify-between gap-3">
              <span class="font-medium text-gray-900 dark:text-white">This conversation</span>
              <span class="tabular-nums text-gray-700 dark:text-gray-300">{{ costLabel() }}</span>
            </div>

            @if (quotaInfo(); as q) {
              <div class="mt-1 flex items-baseline justify-between gap-3">
                <span class="text-gray-700 dark:text-gray-300">{{ q.periodLabel }} quota</span>
                <span class="tabular-nums text-gray-500 dark:text-gray-400">
                  {{ q.usageLabel }} of {{ q.limitLabel }}
                  <span [class]="quotaPctClass()">({{ q.pctLabel }})</span>
                </span>
              </div>
              <div class="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700" aria-hidden="true">
                <span class="block h-full rounded-full" [class]="quotaBarClass()" [style.width.%]="q.barPct"></span>
              </div>
              <span class="text-[11px]/4 text-gray-500 dark:text-gray-400">
                {{ q.remainingLabel }} remaining@if (q.resetInfo) { · {{ q.resetInfo }} }
              </span>
            } @else if (quotaUnlimited()) {
              <span class="text-[11px]/4 text-gray-500 dark:text-gray-400">
                Unlimited quota — no spending limit applies to your account.
              </span>
            }
          </div>
        </div>
      }
  `,
})
export class ContextMeterComponent {
  private readonly chatStateService = inject(ChatStateService);
  private readonly quotaStatusService = inject(QuotaStatusService);
  private readonly injector = inject(Injector);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /**
   * Which session to report on. Omit it — as the main chat does — and the
   * meter follows the *viewed* session, which is the right behaviour for the
   * composer the user is typing into.
   *
   * Pass it for a chat that is on screen without being the viewed session: the
   * Designer's preview and the marketplace test drive stream into their own
   * `preview-` sessions and deliberately never call `setViewedSession`, so an
   * unpinned meter there would report whatever conversation the user last
   * opened — a plausible-looking number belonging to a different conversation,
   * which is worse than none at all.
   */
  readonly sessionId = input<string | null>(null);

  // ---------------------------------------------------------------------------
  // Session reads
  // ---------------------------------------------------------------------------

  protected readonly cost = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.costDollarsFor(id) : this.chatStateService.costDollars();
  });

  protected readonly contextTokens = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.contextTokensFor(id) : this.chatStateService.contextTokens();
  });

  protected readonly contextWindow = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.contextWindowFor(id) : this.chatStateService.contextWindowSize();
  });

  protected readonly contextPctValue = computed(() => {
    const id = this.sessionId();
    return id ? this.chatStateService.contextPctFor(id) : this.chatStateService.contextPct();
  });

  private readonly breakdown = computed(() => {
    const id = this.sessionId();
    return id
      ? this.chatStateService.contextBreakdownFor(id)
      : this.chatStateService.contextBreakdown();
  });

  protected readonly showContext = computed(() => this.contextWindow() > 0);

  protected readonly contextPctClamped = computed(() =>
    Math.min(100, Math.max(0, this.contextPctValue())),
  );

  protected readonly contextLabel = computed(() => {
    const pct = this.contextPctValue();
    if (pct <= 0) return '0%';
    if (pct < 1) return '<1%';
    return `${Math.round(pct)}%`;
  });

  protected readonly showPctInline = computed(() => this.contextPctValue() >= SHOW_PCT_AT);

  protected readonly usedLabel = computed(() => formatTokens(this.contextTokens()));
  protected readonly windowLabel = computed(() => formatTokens(this.contextWindow()));
  protected readonly freeLabel = computed(() =>
    formatTokens(Math.max(0, this.contextWindow() - this.contextTokens())),
  );
  protected readonly costLabel = computed(() => formatUsd(this.cost()));

  protected tokenLabel(value: number): string {
    return formatTokens(value);
  }

  // ---------------------------------------------------------------------------
  // Breakdown
  // ---------------------------------------------------------------------------

  /**
   * The breakdown as bar segments, reconciled with the ring's total.
   *
   * The ring counts what the provider billed for the turn's last call; the
   * breakdown was measured just before that call. They describe the same
   * prompt but come from different counters, so Messages — already the
   * residual partition on the backend — is taken as the residual against the
   * billed total here too. That way the rows add up to the headline figure.
   * If the fixed partitions alone exceed the billed total the two counters
   * disagree about more than rounding, and the breakdown is shown as measured.
   */
  protected readonly segments = computed<MeterSegment[]>(() => {
    const cb = this.breakdown();
    const window = this.contextWindow();
    const used = this.contextTokens();
    if (!cb || !Array.isArray(cb.partitions) || cb.partitions.length === 0 || window <= 0) {
      return [];
    }

    const fixedTokens = cb.partitions
      .filter((p) => p.key !== 'messages')
      .reduce((sum, p) => sum + Math.max(0, p.tokens), 0);
    const reconcile = used > 0 && used >= fixedTokens;

    return cb.partitions
      .map((p) => {
        const tokens =
          p.key === 'messages' && reconcile ? used - fixedTokens : Math.max(0, p.tokens);
        return {
          key: p.key,
          label: p.label,
          tokens,
          pct: (tokens / window) * 100,
          color: PARTITION_COLORS[p.key] ?? FALLBACK_COLOR,
          children: this.childRows(p),
        };
      })
      .filter((s) => s.tokens > 0);
  });

  private childRows(p: ContextPartition): MeterSegment['children'] {
    if (!Array.isArray(p.children)) return [];
    return p.children
      .filter((c) => c.tokens > 0)
      .sort((a, b) => b.tokens - a.tokens)
      .map((c) => ({ key: c.key, label: c.label, tokens: c.tokens }));
  }

  // ---------------------------------------------------------------------------
  // Ring
  // ---------------------------------------------------------------------------

  protected readonly ringSize = 18;
  protected readonly ringCenter = 9;
  protected readonly ringRadius = RING_RADIUS;
  protected readonly ringCircumference = RING_CIRCUMFERENCE;
  protected readonly ringViewBox = '0 0 18 18';

  private readonly ringOffset = computed(
    () => RING_CIRCUMFERENCE * (1 - this.contextPctClamped() / 100),
  );

  // Starts empty so the ring paints unfilled on first render, then fills once
  // the entrance has settled; later changes flow straight through the CSS
  // transition.
  private readonly displayedOffsetSignal = signal(RING_CIRCUMFERENCE);
  protected readonly displayedOffset = this.displayedOffsetSignal.asReadonly();
  /** A zero-length arc still paints its round cap as a dot; hide it instead. */
  protected readonly ringEmpty = computed(
    () => this.displayedOffset() >= RING_CIRCUMFERENCE - 0.01,
  );
  private firstFillScheduled = false;

  protected readonly ringStrokeClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'stroke-state-danger-500 dark:stroke-state-danger-400';
    if (pct >= 70) return 'stroke-state-warning-500 dark:stroke-state-warning-400';
    if (pct >= 50) return 'stroke-state-info-500 dark:stroke-state-info-400';
    return 'stroke-state-success-500 dark:stroke-state-success-400';
  });

  protected readonly pctTextClass = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'font-medium text-state-danger-700 dark:text-state-danger-400';
    if (pct >= 70) return 'font-medium text-state-warning-700 dark:text-state-warning-400';
    return '';
  });

  /** Bar colour when there is no breakdown to split it by. */
  protected readonly usedOnlyColor = computed(() => {
    const pct = this.contextPctValue();
    if (pct >= 90) return 'bg-state-danger-500 dark:bg-state-danger-400';
    if (pct >= 70) return 'bg-state-warning-500 dark:bg-state-warning-400';
    return 'bg-gray-400 dark:bg-gray-500';
  });

  // ---------------------------------------------------------------------------
  // Quota — the user's spending limit behind the conversation's cost.
  // Sourced from GET /costs/quota-status (loaded lazily on first read).
  // ---------------------------------------------------------------------------

  private readonly quotaStatus = this.quotaStatusService.status;

  /** Quota figures for the panel, or null when there's no real limit. */
  protected readonly quotaInfo = computed(() => {
    const status = this.quotaStatus.value();
    if (!status || !status.configured || status.unlimited) return null;
    const limit = status.monthlyLimit;
    if (!limit || limit <= 0) return null;

    const pct = Math.max(0, status.usagePercentage);
    const remaining = status.remaining ?? Math.max(0, limit - status.currentUsage);
    return {
      pct,
      barPct: Math.min(100, pct),
      pctLabel: pct < 1 && pct > 0 ? '<1%' : `${Math.round(pct)}%`,
      periodLabel: status.periodType === 'daily' ? 'Daily' : 'Monthly',
      usageLabel: formatUsd(status.currentUsage),
      limitLabel: formatUsd(limit),
      remainingLabel: formatUsd(remaining),
      resetInfo: status.resetInfo,
    };
  });

  /** True when the user is on an unlimited tier/override (still worth a note). */
  protected readonly quotaUnlimited = computed(() => {
    const status = this.quotaStatus.value();
    return !!status && status.configured && status.unlimited;
  });

  protected readonly quotaPctClass = computed(() => {
    const pct = this.quotaInfo()?.pct ?? 0;
    if (pct >= 90) return 'text-state-danger-700 dark:text-state-danger-400';
    if (pct >= 75) return 'text-state-warning-700 dark:text-state-warning-400';
    return '';
  });

  protected readonly quotaBarClass = computed(() => {
    const pct = this.quotaInfo()?.pct ?? 0;
    if (pct >= 90) return 'bg-state-danger-500 dark:bg-state-danger-400';
    if (pct >= 75) return 'bg-state-warning-500 dark:bg-state-warning-400';
    return 'bg-tertiary-500 dark:bg-tertiary-400';
  });

  // ---------------------------------------------------------------------------
  // Accessible summary — what a screen reader hears on the trigger.
  // ---------------------------------------------------------------------------

  protected readonly triggerAriaLabel = computed(() => {
    const cost = `conversation cost ${this.costLabel()}`;
    if (!this.showContext()) return `Context window not measured yet, ${cost}. Show details`;
    return (
      `Context window ${this.contextLabel()} full ` +
      `(${this.usedLabel()} of ${this.windowLabel()} tokens), ${cost}. Show details`
    );
  });

  // ---------------------------------------------------------------------------
  // Open / close — hover with a mouse, or tap / press to pin it open.
  // ---------------------------------------------------------------------------

  private readonly hovered = signal(false);
  private readonly pinned = signal(false);
  protected readonly open = computed(() => this.hovered() || this.pinned());
  private hoverTimer: ReturnType<typeof setTimeout> | null = null;

  protected onPointerEnter(event: PointerEvent): void {
    // Touch has no hover; a tap arrives as a click and pins instead.
    if (event.pointerType !== 'mouse') return;
    this.clearHoverTimer();
    this.hoverTimer = setTimeout(() => this.hovered.set(true), 150);
  }

  protected onPointerLeave(event: PointerEvent): void {
    if (event.pointerType !== 'mouse') return;
    this.clearHoverTimer();
    this.hoverTimer = setTimeout(() => this.hovered.set(false), 120);
  }

  protected toggle(): void {
    const next = !this.open();
    this.pinned.set(next);
    if (!next) this.hovered.set(false);
  }

  protected close(): void {
    this.clearHoverTimer();
    this.pinned.set(false);
    this.hovered.set(false);
  }

  protected onDocumentClick(event: MouseEvent): void {
    if (!this.pinned()) return;
    const target = event.target as Node | null;
    if (target && !this.host.nativeElement.contains(target)) this.close();
  }

  private clearHoverTimer(): void {
    if (this.hoverTimer !== null) {
      clearTimeout(this.hoverTimer);
      this.hoverTimer = null;
    }
  }

  constructor() {
    effect((onCleanup) => {
      // Reset to empty when the ring is hidden so the next mount fills again.
      if (!this.showContext()) {
        this.displayedOffsetSignal.set(RING_CIRCUMFERENCE);
        this.firstFillScheduled = false;
        return;
      }

      const target = this.ringOffset();
      if (this.firstFillScheduled) {
        this.displayedOffsetSignal.set(target);
        return;
      }
      this.firstFillScheduled = true;
      let timer: ReturnType<typeof setTimeout> | null = null;
      afterNextRender(
        () => {
          timer = setTimeout(() => this.displayedOffsetSignal.set(this.ringOffset()), RING_FILL_DELAY_MS);
        },
        { injector: this.injector },
      );
      onCleanup(() => {
        if (timer !== null) clearTimeout(timer);
      });
    });

    inject(DestroyRef).onDestroy(() => this.clearHoverTimer());
  }
}
