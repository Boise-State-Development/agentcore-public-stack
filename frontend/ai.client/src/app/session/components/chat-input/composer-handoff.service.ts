import { Injectable } from '@angular/core';

/** A handoff older than this was not the send that mounted this composer. */
const HANDOFF_WINDOW_MS = 2000;

/**
 * Carries the full composer's height across the first send, so the compact
 * composer that replaces it can shrink from that height instead of popping in.
 *
 * The two are different component instances: the first send navigates from
 * `/` to `/s/:id`, which rebuilds the session page and everything in it. So
 * there is no single element whose height could be transitioned — the outgoing
 * composer leaves a measurement here, and the incoming one takes it on mount.
 *
 * Take-once and time-boxed: a measurement that is never taken (the send
 * failed, the user navigated away) must not animate some later, unrelated
 * composer.
 */
@Injectable({ providedIn: 'root' })
export class ComposerHandoffService {
  private handoff: { height: number; at: number } | null = null;

  /** The full composer is sending; remember how tall it was. */
  leave(height: number): void {
    this.handoff = height > 0 ? { height, at: Date.now() } : null;
  }

  /** The height to animate from, if a full composer just handed off. */
  take(): number | null {
    const handoff = this.handoff;
    this.handoff = null;
    if (!handoff || Date.now() - handoff.at > HANDOFF_WINDOW_MS) return null;
    return handoff.height;
  }
}
