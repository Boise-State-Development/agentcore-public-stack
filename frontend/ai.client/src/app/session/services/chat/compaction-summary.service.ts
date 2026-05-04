import { Injectable, computed, signal } from '@angular/core';
import type { CompactionEvent } from '../../../shared/utils/stream-parser';

/**
 * Aggregate compaction state for the current session view. Earlier
 * iterations of this work tracked per-message-index dividers; that was
 * dropped because the inline placement caused jarring layout shifts when
 * the divider dropped between messages mid-conversation. The new shape
 * is a single end-of-conversation summary, so we only need totals.
 *
 * `totalSummarizedTurns` is the running sum of `summarizedTurns` across
 * every `compaction` SSE event received this session. On session load the
 * persisted cumulative count is replayed via `seedFromHydration`, which
 * also flips `wasHydrated` so the indicator renders without re-playing
 * the one-shot fade-in.
 */
@Injectable({
  providedIn: 'root',
})
export class CompactionSummaryService {
  private readonly turnsSignal = signal<number>(0);
  private readonly hydratedSignal = signal<boolean>(false);

  readonly totalSummarizedTurns = this.turnsSignal.asReadonly();

  /** True when the current total came from session-metadata hydration and
   *  no live `compaction` event has fired since. The component reads this
   *  to suppress the fade-in animation on reload. */
  readonly wasHydrated = this.hydratedSignal.asReadonly();

  /** Whether the summary should render at all in the UI. */
  readonly hasCompaction = computed(() => this.turnsSignal() > 0);

  /**
   * Record a live `compaction` SSE event. Adds to the running total and
   * clears the hydrated flag so the indicator animates in.
   */
  recordLive(event: CompactionEvent): void {
    this.turnsSignal.update((total) => total + event.summarizedTurns);
    this.hydratedSignal.set(false);
  }

  /**
   * Replay a persisted cumulative total on session load. Idempotent and
   * non-clobbering: a stale hydration call that arrives after a live
   * event has already incremented the total is dropped.
   */
  seedFromHydration(total: number): void {
    if (total <= 0) return;
    if (this.turnsSignal() > 0 || this.hydratedSignal()) return;
    this.turnsSignal.set(total);
    this.hydratedSignal.set(true);
  }

  /** Clear all state — called on session change. */
  reset(): void {
    this.turnsSignal.set(0);
    this.hydratedSignal.set(false);
  }
}
