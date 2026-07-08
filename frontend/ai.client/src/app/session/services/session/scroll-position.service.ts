import { Injectable } from '@angular/core';

/**
 * In-memory scroll position per conversation, for the lifetime of the SPA
 * session. Backs the navigation scroll policy (see ConversationPage):
 * navigating back to a conversation restores where the user was; a
 * conversation with no remembered position (first open, or after a full
 * reload) lands on its latest turn instead.
 *
 * Deliberately not persisted — after a reload the message heights, fonts
 * and hydrated cards can differ enough that a stored pixel offset points
 * somewhere arbitrary, which is worse than the deterministic latest-turn
 * anchor.
 */
@Injectable({ providedIn: 'root' })
export class ScrollPositionService {
  private readonly positions = new Map<string, number>();

  /** Remember the window scroll offset for a conversation. */
  save(sessionId: string, scrollY: number): void {
    this.positions.set(sessionId, scrollY);
  }

  /** The remembered offset, or undefined if this conversation has none. */
  get(sessionId: string): number | undefined {
    return this.positions.get(sessionId);
  }

  /** Drop a conversation's remembered offset. */
  clear(sessionId: string): void {
    this.positions.delete(sessionId);
  }
}
