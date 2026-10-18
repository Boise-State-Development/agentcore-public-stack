import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroArrowsPointingIn,
  heroArrowsPointingOut,
  heroCheck,
  heroExclamationTriangle,
  heroLockClosed,
  heroXMark,
} from '@ng-icons/heroicons/outline';

import {
  BrowserLiveView,
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';
import { BrowserPaneStateService } from '../../../../services/browser-pane/browser-pane-state.service';
import { DockedPaneService } from '../../../../services/docked-pane/docked-pane.service';
import { hostLabel } from '../browser-card/browser-page';

/**
 * Rail width beyond the remote viewport: the frame container's `p-3` on both
 * sides plus the rail's border, rounded up. A rail this much wider than the
 * stream shows it at exactly 1:1.
 */
export const BROWSER_RAIL_GUTTER_PX = 32;

/** The chat column the rail never squeezes below — same floor as a drag. */
export const CHAT_FLOOR_PX = 360;

/**
 * Right-docked pane holding the live browser the user drives to sign in
 * (`docs/specs/authenticated-web-assessment.md`). Replaces the inline viewer
 * and its full-screen overlay: the stream now opens beside the conversation,
 * on the same rail as artifacts and file previews, so the user can read what
 * the agent asked for while they type.
 *
 * Shares the rail through `DockedPaneService` — same width, same resize
 * affordance, same side-nav choreography as `ArtifactPanelComponent` — and
 * is mounted once per message list, so the iframe never moves in the DOM.
 * (Moving an iframe reloads it, which tears down the DCV stream; that is why
 * this is not a CDK dialog or portal.)
 *
 * What carried over from the inline viewer, and why it matters
 * ------------------------------------------------------------
 * **The URL is never held on the request.** A live-view URL is SigV4
 * query-signed and lives at most 300 seconds, so it is minted when the pane
 * opens and re-minted before it expires.
 *
 * **The viewer is framed from another origin.** The mcp-sandbox origin's
 * CloudFront function locks `frame-ancestors` to this SPA and composes
 * `connect-src` from the `?csp=` query — which is what lets the page open
 * DCV's WebSocket. The SPA posts the URL *into* the frame rather than letting
 * the frame call app-api: that origin also hosts untrusted MCP App HTML.
 *
 * **There is a deadline.** Past it the backend has released the browser, so
 * the pane closes and the card in the conversation says why.
 *
 * Escape deliberately does not close this pane, unlike its siblings: the user
 * is typing into a remote page, and a stray Escape that tore down the stream
 * mid-password would cost a re-mint and their place.
 *
 * Scale: 1:1, never larger
 * ------------------------
 * The backend hands the browser over at a sign-in size (768 wide, set before
 * the automation stream is disabled, since CDP is refused after), and the
 * event's `viewport` says what it chose. Its height is this panel's frame
 * height, which every chat request carries (`browser-frame-height.ts`, whose
 * chrome constant must follow this template's header and disclosure). On open the rail widens to fit that
 * width, when the window has room for the chat beside it, and goes back to
 * the user's own width on close. The frame is never drawn wider than the
 * remote viewport: past 1:1 the page only gets blurrier, so a wider rail
 * centres the frame and a narrower one scales it down.
 *
 * **Expand** shows the same stream over the whole window, by restyling the
 * pane in place. The iframe is not moved, for the reason above; collapsing
 * puts it back on the rail with the stream still up.
 */
@Component({
  selector: 'app-browser-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, A11yModule],
  providers: [
    provideIcons({
      heroArrowPath,
      heroArrowsPointingIn,
      heroArrowsPointingOut,
      heroCheck,
      heroExclamationTriangle,
      heroLockClosed,
      heroXMark,
    }),
  ],
  template: `
    @if (request(); as req) {
      <!-- Expanded is the same element restyled to cover the window, never a
           second viewer or a moved one: moving the iframe reloads it. While
           expanded it is a modal surface, so it takes the dialog role and
           traps focus; docked, it is neither. -->
      <aside
        class="fixed inset-y-0 right-0 flex w-full flex-col border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900"
        [class.z-40]="!expanded()"
        [class.z-60]="expanded()"
        [class.left-0]="expanded()"
        [class.border-l]="!expanded()"
        [style.maxWidth]="expanded() ? null : paneWidthCss()"
        [class.select-none]="dragging()"
        [attr.role]="expanded() ? 'dialog' : null"
        [attr.aria-modal]="expanded() ? 'true' : null"
        [attr.aria-label]="'Browser: ' + title()"
        [cdkTrapFocus]="expanded()"
        (keydown.escape)="onEscape()"
      >
        <div
          [class.hidden]="expanded()"
          role="separator"
          aria-orientation="vertical"
          tabindex="0"
          aria-label="Resize browser panel"
          [attr.aria-valuemin]="paneWidthMin"
          [attr.aria-valuemax]="paneWidthMax"
          [attr.aria-valuenow]="paneWidth()"
          class="group absolute inset-y-0 left-0 z-10 flex w-2 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center focus-visible:outline-none"
          (pointerdown)="onHandlePointerDown($event)"
          (pointermove)="onHandlePointerMove($event)"
          (pointerup)="onHandlePointerUp($event)"
          (pointercancel)="onHandlePointerUp($event)"
          (keydown)="onHandleKeydown($event)"
        >
          <span
            aria-hidden="true"
            class="h-12 w-1 rounded-full bg-gray-300 transition-colors group-hover:bg-primary-500 group-focus-visible:bg-primary-500 dark:bg-gray-600 dark:group-hover:bg-primary-400"
          ></span>
        </div>

        <header
          class="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700"
        >
          <span
            class="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-600 dark:bg-white/10 dark:text-gray-300"
            aria-hidden="true"
          >
            <ng-icon name="heroLockClosed" size="16" />
          </span>
          <div class="min-w-0 flex-1">
            <h2
              class="truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
            >
              {{ title() }}
            </h2>
            <p class="truncate text-xs text-gray-500 dark:text-gray-400">
              You're driving{{ remainingLabel() ? ' · ' + remainingLabel() + ' left' : '' }}
            </p>
          </div>
          <button
            type="button"
            class="inline-flex shrink-0 items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm/6 font-medium text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible"
            (click)="finish(req)"
          >
            <ng-icon name="heroCheck" size="16" />
            I've signed in
          </button>
          <button
            #expandToggle
            type="button"
            class="flex size-8 shrink-0 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            [attr.aria-label]="expanded() ? 'Return browser to the side panel' : 'Expand browser to full window'"
            [attr.title]="expanded() ? 'Collapse' : 'Expand'"
            (click)="toggleExpanded()"
          >
            <ng-icon
              [name]="expanded() ? 'heroArrowsPointingIn' : 'heroArrowsPointingOut'"
              size="18"
            />
          </button>
          <button
            type="button"
            class="flex size-8 shrink-0 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-accessible dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-100"
            aria-label="Close browser panel"
            (click)="close()"
          >
            <ng-icon name="heroXMark" size="18" />
          </button>
        </header>

        <!-- Disclosure, not decoration. The user is about to type a password
             into a browser running in our AWS account; saying so plainly is
             the minimum, and it is also the honest framing of what the agent
             can and cannot see. -->
        <p
          class="border-b border-gray-200 bg-gray-50 px-4 py-2 text-xs/5 text-gray-600 dark:border-gray-700 dark:bg-white/[0.03] dark:text-gray-300"
        >
          This browser runs in our cloud. The assistant is locked out while
          you're signed in, and it never sees what you type.
        </p>

        <!-- Container-sized so the frame can letterbox: DCV's
             remoteWidth/remoteHeight must match the browser session or the
             stream crops, so the frame keeps the event's aspect ratio at
             whatever width the user drags the rail to, and stops growing at
             the remote viewport's own size. -->
        <div
          class="relative flex min-h-0 flex-1 items-center justify-center bg-gray-100 p-3 [container-type:size] dark:bg-gray-950"
          [class.pointer-events-none]="dragging()"
        >
          @if (error(); as message) {
            <div
              class="flex max-w-sm flex-col items-center gap-3 text-center"
              role="alert"
            >
              <span class="text-state-warning-500" aria-hidden="true">
                <ng-icon name="heroExclamationTriangle" size="28" />
              </span>
              <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                {{ message }}
              </p>
              <button
                type="button"
                class="rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-medium text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible"
                (click)="retry()"
              >
                Try again
              </button>
            </div>
          } @else if (frameSrc(); as src) {
            <div
              class="relative overflow-hidden rounded-lg bg-black shadow-sm ring-1 ring-gray-900/10 dark:ring-white/10"
              [style.aspectRatio]="aspectRatio()"
              [style.width]="frameWidth()"
            >
              <iframe
                #viewerFrame
                class="absolute inset-0 h-full w-full border-0"
                title="Browser sign-in"
                [src]="src"
                (load)="onFrameLoad()"
              ></iframe>
            </div>
          } @else {
            <div
              class="flex flex-col items-center gap-3 text-gray-500 dark:text-gray-400"
              aria-live="polite"
            >
              <span class="motion-safe:animate-spin" aria-hidden="true">
                <ng-icon name="heroArrowPath" size="24" />
              </span>
              <p class="text-sm">Connecting to the browser…</p>
            </div>
          }
        </div>
      </aside>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class BrowserPanelComponent {
  private readonly pane = inject(BrowserPaneStateService);
  private readonly dockedPane = inject(DockedPaneService);
  private readonly service = inject(BrowserLoginService);
  private readonly sanitizer = inject(DomSanitizer);

  protected readonly request = this.pane.openRequest;

  /**
   * The id alone, compared by value: the mint effect keys on this so a
   * re-created request object for the same prompt never mints a second
   * live credential.
   */
  private readonly requestId = computed(() => this.request()?.interruptId ?? null);

  protected readonly error = signal<string | null>(null);

  /** Ticks while the pane is open, so the countdown and lapse re-evaluate. */
  private readonly now = signal(Date.now());

  private readonly viewerFrame =
    viewChild<ElementRef<HTMLIFrameElement>>('viewerFrame');

  private pending: BrowserLiveView | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  /** Bumped per open so a mint that resolves after a close or switch is dropped. */
  private mintSeq = 0;

  /** Origin of the minted live-view URL, for the frame's `connect-src`. */
  private readonly streamOrigin = signal<string | null>(null);

  protected readonly title = computed(() => {
    const target = this.request()?.targetUrl;
    const host = target ? hostLabel(target) : null;
    return host ? `Sign in to ${host}` : 'Sign in to continue';
  });

  protected readonly remainingLabel = computed<string | null>(() => {
    const deadline = this.request()?.deadlineAt;
    if (!deadline) return null;
    const ms = Date.parse(deadline) - this.now();
    if (!Number.isFinite(ms) || ms <= 0) return null;
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
  });

  protected readonly aspectRatio = computed(() => {
    const viewport = this.request()?.viewport;
    return viewport && viewport.height > 0
      ? `${viewport.width} / ${viewport.height}`
      : '16 / 10';
  });

  /**
   * As wide as the pane allows without the height overflowing it, and never
   * wider than the remote viewport: a stream drawn past 1:1 is only an
   * upscaled, softer copy of the same pixels.
   */
  protected readonly frameWidth = computed(() => {
    const viewport = this.request()?.viewport;
    const ratio =
      viewport && viewport.height > 0 ? viewport.width / viewport.height : 1.6;
    const fit = `100cqw, calc(100cqh * ${ratio.toFixed(4)})`;
    return viewport && viewport.width > 0
      ? `min(${fit}, ${viewport.width}px)`
      : `min(${fit})`;
  });

  /** Whether the stream is shown over the whole window rather than the rail. */
  protected readonly expanded = signal(false);

  private readonly expandToggle =
    viewChild<ElementRef<HTMLButtonElement>>('expandToggle');

  /**
   * The rail width from before this pane widened it, and the width it set.
   * Null when the pane left the rail alone (it was already wide enough, or
   * the window had no room).
   */
  private railBefore: { width: number; fitted: number } | null = null;

  /**
   * The viewer page, with the CSP it needs declared in the query.
   *
   * Derived from the **minted URL's own origin** rather than a wildcard like
   * `https://*.amazonaws.com`, so the grant is as narrow as the thing it is
   * for. Null until the mint lands, which is what keeps the frame from
   * rendering as an empty black box when minting fails.
   */
  protected readonly frameSrc = computed<SafeResourceUrl | null>(() => {
    const origin = this.request()?.sandboxOrigin;
    const stream = this.streamOrigin();
    if (!origin || !stream) return null;
    const csp = encodeURIComponent(
      JSON.stringify({
        connectDomains: [stream, stream.replace(/^https:/, 'wss:')],
      }),
    );
    return this.sanitizer.bypassSecurityTrustResourceUrl(
      `${origin}/live-view.html?csp=${csp}`,
    );
  });

  // Rail width — shared with the other docked panes.
  protected readonly paneWidth = this.dockedPane.width;
  protected readonly paneWidthMin = this.dockedPane.widthMin;
  protected readonly paneWidthMax = this.dockedPane.widthMax;
  protected readonly paneWidthCss = computed(
    () => `${this.dockedPane.width()}px`,
  );
  protected readonly dragging = signal(false);
  private dragStartX = 0;
  private dragStartWidth = 0;

  constructor() {
    // The viewer announces itself once its listener is attached. Accepting it
    // only from the sandbox origin keeps any other frame on the page from
    // provoking us into posting a live signed URL.
    const onMessage = (event: MessageEvent) => {
      const origin = this.request()?.sandboxOrigin;
      if (!origin || event.origin !== origin) return;
      const data = event.data as { type?: unknown } | null;
      if (!data || data.type !== 'browser-live-view/ready') return;
      this.postToFrame();
    };
    window.addEventListener('message', onMessage);

    inject(DestroyRef).onDestroy(() => {
      window.removeEventListener('message', onMessage);
      this.teardown();
      // Leaving for a page with no message list must give the rail back,
      // or the shell keeps its gutter.
      this.pane.close();
    });

    // One mint per opened request. Keyed on the interrupt id so the countdown
    // ticking (which re-creates nothing) never re-mints, and switching to a
    // different prompt tears the old stream down first.
    effect((onCleanup) => {
      if (!this.requestId()) return;
      untracked(() => {
        this.fitRail();
        void this.mint();
      });
      const tick = setInterval(() => this.now.set(Date.now()), 1000);
      onCleanup(() => {
        clearInterval(tick);
        this.teardown();
        this.expanded.set(false);
        this.restoreRail();
      });
    });

    // The rail is claimed but its request is gone (resolved here or from
    // another tab), or the sign-in window closed: give the rail back. The
    // card in the conversation carries the lapsed message.
    effect(() => {
      const holds = this.pane.openInterruptId() !== null;
      const req = this.request();
      if (!holds) return;
      if (!req || this.service.hasLapsed(req, this.now()) || !req.sandboxOrigin) {
        untracked(() => this.pane.close());
      }
    });
  }

  private async mint(): Promise<void> {
    const req = this.request();
    if (!req) return;
    const seq = ++this.mintSeq;
    this.error.set(null);
    try {
      // Mint before showing the frame so a failure surfaces as a message
      // rather than an empty black box.
      const view = await this.service.mintLiveView(req.sessionId);
      if (seq !== this.mintSeq) return;
      this.pending = view;
      try {
        this.streamOrigin.set(new URL(view.url).origin);
      } catch {
        this.error.set('The sign-in session address was not usable.');
        return;
      }
      // The frame may not exist yet on this tick, so this call, the iframe's
      // `load` and the viewer's own `ready` all post — whichever arrives first
      // is the one that counts. The viewer is idempotent on the connection
      // attempt for exactly this reason.
      this.postToFrame();
      this.scheduleRefresh(view.expiresAt);
    } catch {
      if (seq !== this.mintSeq) return;
      this.error.set(
        'Could not open the sign-in viewer. The browser session may have ended.',
      );
    }
  }

  protected retry(): void {
    void this.mint();
  }

  protected onFrameLoad(): void {
    this.postToFrame();
  }

  /**
   * Hand the freshly minted URL to the viewer.
   *
   * Targeted at the sandbox origin explicitly rather than `'*'`: that origin
   * also serves untrusted MCP App HTML, and a wildcard target would post a
   * live signed URL to whatever happened to be in the frame.
   */
  private postToFrame(): void {
    const origin = this.request()?.sandboxOrigin;
    const target = this.viewerFrame()?.nativeElement?.contentWindow;
    if (!target || !this.pending || !origin) return;
    target.postMessage(
      {
        type: 'browser-live-view/connect',
        url: this.pending.url,
        viewport: this.pending.viewport,
      },
      origin,
    );
  }

  /**
   * Re-mint before the signature expires. A sign-in routinely outlives the
   * 300-second cap, so without this the stream dies mid-password.
   */
  private scheduleRefresh(expiresAt: string): void {
    this.clearRefresh();
    const ms = Date.parse(expiresAt) - Date.now();
    if (!Number.isFinite(ms)) return;
    // Refresh with a margin, and never busy-loop on an already-stale value.
    const delay = Math.max(ms - 30_000, 5_000);
    const seq = this.mintSeq;
    this.refreshTimer = setTimeout(() => void this.refresh(seq), delay);
  }

  private async refresh(seq: number): Promise<void> {
    const req = this.request();
    if (!req || seq !== this.mintSeq || this.service.hasLapsed(req)) return;
    try {
      const view = await this.service.mintLiveView(req.sessionId);
      if (seq !== this.mintSeq) return;
      this.pending = view;
      this.postToFrame();
      this.scheduleRefresh(view.expiresAt);
    } catch {
      // Leave the current stream up; it may still have time on it, and a
      // transient failure is not worth tearing the user's session down.
    }
  }

  private clearRefresh(): void {
    if (this.refreshTimer !== null) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  private teardown(): void {
    this.mintSeq++;
    this.clearRefresh();
    this.pending = null;
    this.streamOrigin.set(null);
    this.error.set(null);
  }

  protected close(): void {
    this.pane.close();
  }

  /**
   * Widen the rail so the stream fits at 1:1, if the window has room.
   *
   * Only ever widens: a user whose rail is already wider keeps it, and the
   * frame centres. Bounded by the same 360px chat floor a drag is, so a small
   * window gets as much as it can spare and the frame scales down the rest.
   */
  private fitRail(): void {
    const viewport = this.request()?.viewport;
    if (!viewport || viewport.width <= 0 || typeof window === 'undefined') return;
    const before = this.dockedPane.width();
    const room = window.innerWidth - CHAT_FLOOR_PX;
    const target = Math.min(viewport.width + BROWSER_RAIL_GUTTER_PX, room);
    if (target <= before) return;
    this.dockedPane.setWidth(target);
    this.railBefore = { width: before, fitted: this.dockedPane.width() };
  }

  /**
   * Give the rail back at the width the user had before it opened.
   *
   * Unless they resized it while it was open: that width is their choice
   * now, and the rail keeps a user-set width across panes on purpose.
   */
  private restoreRail(): void {
    const saved = this.railBefore;
    this.railBefore = null;
    if (!saved || this.dockedPane.width() !== saved.fitted) return;
    this.dockedPane.setWidth(saved.width);
  }

  protected toggleExpanded(): void {
    this.expanded.update((v) => !v);
    // The toggle is the one control in both layouts, so focus stays on it:
    // keyboard users land where they can collapse again, inside the trap.
    queueMicrotask(() => this.expandToggle()?.nativeElement.focus());
  }

  /**
   * Escape collapses the expanded view, and does nothing else. It never
   * closes the pane (see the class comment), and a key pressed inside the
   * remote page never reaches here anyway: the iframe is its own document.
   */
  protected onEscape(): void {
    if (this.expanded()) this.toggleExpanded();
  }

  protected async finish(req: BrowserLoginRequest): Promise<void> {
    this.pane.close();
    await this.service.complete(req.interruptId);
  }

  protected onHandlePointerDown(e: PointerEvent): void {
    e.preventDefault();
    try {
      (e.target as Element).setPointerCapture(e.pointerId);
    } catch {
      /* not all pointer types/environments allow capture — drag still works */
    }
    this.dragStartX = e.clientX;
    this.dragStartWidth = this.dockedPane.width();
    this.dragging.set(true);
  }

  protected onHandlePointerMove(e: PointerEvent): void {
    if (!this.dragging()) return;
    // Docked to the right edge: dragging left (a smaller clientX) widens it.
    this.applyWidth(this.dragStartWidth + (this.dragStartX - e.clientX));
  }

  protected onHandlePointerUp(e: PointerEvent): void {
    if (!this.dragging()) return;
    this.dragging.set(false);
    try {
      const el = e.target as Element;
      if (el.hasPointerCapture(e.pointerId)) {
        el.releasePointerCapture(e.pointerId);
      }
    } catch {
      /* capture may never have been established — nothing to release */
    }
  }

  protected onHandleKeydown(e: KeyboardEvent): void {
    const step = e.shiftKey ? 64 : 16;
    const w = this.dockedPane.width();
    switch (e.key) {
      case 'ArrowLeft':
        this.applyWidth(w + step);
        break;
      case 'ArrowRight':
        this.applyWidth(w - step);
        break;
      case 'Home':
        this.applyWidth(this.paneWidthMax);
        break;
      case 'End':
        this.applyWidth(this.paneWidthMin);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  /** Same 360px floor for the chat as the other docked panes. */
  private applyWidth(px: number): void {
    let target = px;
    if (typeof window !== 'undefined') {
      target = Math.min(target, Math.max(this.paneWidthMin, window.innerWidth - 360));
    }
    this.dockedPane.setWidth(target);
  }
}
