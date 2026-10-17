import { Injectable, computed, inject, signal } from '@angular/core';
import { DockedPaneService } from '../docked-pane/docked-pane.service';
import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../services/browser-login/browser-login.service';

/**
 * Open-pane state for the docked browser sign-in viewer.
 *
 * Holds the *interrupt id* being viewed, not the request: the request lives
 * in `BrowserLoginService.pending`, and reading it back from there means a
 * prompt that resolves (signed in, skipped, resumed from another tab) drops
 * out of the pane on its own instead of leaving a viewer onto a browser the
 * backend has already taken back.
 *
 * Shares the one right-docked rail with the artifact pane and the file
 * preview via `DockedPaneService`, and gates `openRequest` on the rail's
 * owner for the same reason they do: eviction is a read, not a callback.
 * Evicting this pane destroys the viewer's iframe, which drops the stream;
 * reopening mints a fresh URL, so nothing is lost but a second or two.
 */
@Injectable({ providedIn: 'root' })
export class BrowserPaneStateService {
  private readonly dockedPane = inject(DockedPaneService);
  private readonly browserLogin = inject(BrowserLoginService);

  private readonly openId = signal<string | null>(null);

  /** The interrupt the pane is showing, or null when closed or evicted. */
  readonly openInterruptId = computed<string | null>(() =>
    this.dockedPane.owner() === 'browser' ? this.openId() : null,
  );

  /**
   * The sign-in request on screen. Null when closed, evicted, or when the
   * request has since been resolved — the panel releases the rail on that
   * last case, since the rail is still claimed.
   */
  readonly openRequest = computed<BrowserLoginRequest | null>(() => {
    const id = this.openInterruptId();
    if (!id) return null;
    return this.browserLogin.pending().find((r) => r.interruptId === id) ?? null;
  });

  open(request: BrowserLoginRequest): void {
    this.openId.set(request.interruptId);
    this.dockedPane.claim('browser');
  }

  close(): void {
    this.openId.set(null);
    this.dockedPane.release('browser');
  }

  /** Clear on session change: the browser belongs to the conversation being left. */
  reset(): void {
    this.close();
  }
}
