import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationStart, Router } from '@angular/router';
import { APP_SCROLL_CONTAINER_ID } from '../../../services/route-scroll/route-scroll.service';

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
 *
 * The offset is captured at `NavigationStart`, while the conversation is
 * still on screen. Any later is too late: when the router deactivates the
 * page it removes the page's DOM *before* running its `ngOnDestroy`, so by
 * then the scroll container has lost its content and the browser has
 * already clamped the offset to 0.
 */
@Injectable({ providedIn: 'root' })
export class ScrollPositionService {
  private readonly document = inject(DOCUMENT);
  private readonly positions = new Map<string, number>();

  /**
   * The conversation on screen, once its viewport has been positioned
   * (remembered offset restored, or latest turn anchored). Null while
   * none is, including while one is still loading: leaving then must not
   * record the transient top-of-page offset over a remembered spot.
   */
  private tracked: string | null = null;

  constructor() {
    if (!isPlatformBrowser(inject(PLATFORM_ID))) return;
    inject(Router)
      .events.pipe(takeUntilDestroyed())
      .subscribe((event) => {
        if (event instanceof NavigationStart) this.captureTracked();
      });
  }

  /** Remember the scroll offset for a conversation. */
  save(sessionId: string, scrollTop: number): void {
    this.positions.set(sessionId, scrollTop);
  }

  /** The remembered offset, or undefined if this conversation has none. */
  get(sessionId: string): number | undefined {
    return this.positions.get(sessionId);
  }

  /** Drop a conversation's remembered offset. */
  clear(sessionId: string): void {
    this.positions.delete(sessionId);
  }

  /**
   * The conversation's viewport is positioned: from here on, every
   * navigation start remembers its offset, until `untrack`.
   */
  track(sessionId: string): void {
    this.tracked = sessionId;
  }

  /** Stop remembering offsets: the conversation is changing, or leaving the screen. */
  untrack(): void {
    this.tracked = null;
  }

  private captureTracked(): void {
    const container = this.document.getElementById(APP_SCROLL_CONTAINER_ID);
    if (this.tracked && container) this.save(this.tracked, container.scrollTop);
  }
}
