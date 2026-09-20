import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowTopRightOnSquare,
  heroArrowsPointingIn,
  heroArrowsPointingOut,
  heroCheck,
  heroLockClosed,
} from '@ng-icons/heroicons/outline';

import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';
import { SpinnerComponent } from '../../../../../components/spinner/spinner.component';

/**
 * Inline prompt for a turn the agent paused so the user can sign in to a site
 * it cannot reach (`docs/specs/authenticated-web-assessment.md`).
 *
 * Sibling of `UserQuestionPromptComponent` and deliberately the same visual
 * language — full width, soft ground, no hard border — because it asks for the
 * same kind of attention: something the reader has to act on, not a yes/no to
 * get out of the way.
 *
 * What is different, and why
 * --------------------------
 * **The URL is never held.** A live-view URL is SigV4 query-signed and lives at
 * most 300 seconds, so it is minted when the viewer opens and re-minted before
 * it expires. Nothing on `request` carries one.
 *
 * **The viewer is framed from another origin.** The mcp-sandbox origin's
 * CloudFront function locks `frame-ancestors` to this SPA and composes
 * `connect-src` from the `?csp=` query — which is what lets the page open DCV's
 * WebSocket. The SPA mints and posts the URL *into* the frame rather than
 * letting the frame call app-api: that origin also hosts untrusted MCP App
 * HTML, and giving it credentialed access to app-api would hand every App a
 * path to the user's session.
 *
 * **There is a deadline.** Past it the backend has released the browser and let
 * the session become reapable, so the viewer stops being offered and the only
 * honest action left is to dismiss.
 *
 * Two house traps, same as the sibling component — don't reintroduce either:
 * dark rules use `:host-context(.dark)` (Angular's emulated encapsulation makes
 * `:where(.dark, .dark *)` never match), and utility classes on `<ng-icon>` do
 * not apply — colour the wrapper and let the icon inherit `currentColor`.
 */
@Component({
  selector: 'app-browser-login-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, SpinnerComponent],
  providers: [
    provideIcons({
      heroArrowTopRightOnSquare,
      heroArrowsPointingIn,
      heroArrowsPointingOut,
      heroCheck,
      heroLockClosed,
    }),
  ],
  // Escape leaves full screen. Bound on the document because focus is usually
  // inside the cross-origin viewer iframe, where a host-element binding would
  // never see the key.
  host: { class: 'block', '(document:keydown.escape)': 'collapse()' },
  template: `
    <section
      class="w-full rounded-2xl bg-gray-50/80 p-5 ring-1 ring-gray-200/70 dark:bg-white/[0.035] dark:ring-white/10"
      aria-label="Sign-in required to continue"
    >
      <div class="flex items-start gap-3">
        <span
          class="mt-0.5 shrink-0 text-gray-500 dark:text-gray-400"
          aria-hidden="true"
        >
          <ng-icon name="heroLockClosed" size="20" />
        </span>
        <div class="min-w-0 flex-1">
          <p class="text-sm/6 font-medium text-gray-900 dark:text-gray-100">
            Sign in to continue
          </p>
          @if (request().reason; as reason) {
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">
              {{ reason }}
            </p>
          }
          @if (request().targetUrl; as target) {
            <p
              class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400"
              [title]="target"
            >
              {{ target }}
            </p>
          }

          <!-- Disclosure, not decoration. The user is about to type a password
               into a browser running in our AWS account; saying so plainly is
               the minimum, and it is also the honest framing of what the agent
               can and cannot see. -->
          <p class="mt-3 text-xs/5 text-gray-500 dark:text-gray-400">
            You'll drive a browser running in our cloud. The assistant is locked
            out while you're signed in, and it never sees what you type.
          </p>

          @if (lapsed()) {
            <p
              class="mt-3 text-sm/6 text-state-warning-700 dark:text-state-warning-400"
              role="status"
            >
              The sign-in window closed, so the browser was released. Send your
              message again to start a new one.
            </p>
          } @else if (!canFrame()) {
            <p
              class="mt-3 text-sm/6 text-state-warning-700 dark:text-state-warning-400"
              role="status"
            >
              The sign-in viewer isn't available in this environment.
            </p>
          } @else if (remainingLabel(); as remaining) {
            <p class="mt-3 text-xs/5 text-gray-500 dark:text-gray-400">
              {{ remaining }} left to finish.
            </p>
          }

          @if (error(); as message) {
            <p
              class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400"
              role="alert"
            >
              {{ message }}
            </p>
          }

          <div class="mt-4 flex flex-wrap items-center gap-2">
            @if (!lapsed() && canFrame() && !open()) {
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-3.5 py-2 text-sm/6 font-medium text-white hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible disabled:opacity-60"
                [disabled]="minting()"
                (click)="openViewer()"
              >
                @if (minting()) {
                  <app-spinner size="sm" />
                } @else {
                  <ng-icon name="heroArrowTopRightOnSquare" size="16" />
                }
                Open sign-in
              </button>
            }
            @if (open()) {
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-3.5 py-2 text-sm/6 font-medium text-white hover:opacity-90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible"
                (click)="finish()"
              >
                <ng-icon name="heroCheck" size="16" />
                I've signed in
              </button>
            }
            <button
              type="button"
              class="rounded-2xl px-3.5 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-400 dark:text-gray-200 dark:hover:bg-white/10"
              (click)="dismiss()"
            >
              {{ lapsed() ? 'Dismiss' : "Don't sign in" }}
            </button>
          </div>
        </div>
      </div>

      @if (open() && frameSrc(); as src) {
        <!-- The frame is ONE element in both states: expanding must not
             re-create the iframe, or the DCV stream tears down and the user
             loses a half-typed password. Only the wrapper's classes change.
             This is also why the expanded state is NOT a CDK Dialog, as the
             Angular guide otherwise requires: a dialog (or a CDK portal)
             re-parents the content, and browsers reload an iframe when it
             moves in the DOM — which would drop the stream every time the user
             expanded it. Escape, an explicit close and the ARIA roles are
             wired by hand instead. -->
        <div
          [class]="
            expanded()
              ? 'fixed inset-0 z-50 flex flex-col bg-gray-950/95 p-4 backdrop-blur-sm'
              : 'mt-4'
          "
          [attr.role]="expanded() ? 'dialog' : null"
          [attr.aria-modal]="expanded() ? 'true' : null"
          [attr.aria-label]="expanded() ? 'Browser sign-in, full screen' : null"
        >
          <div
            class="flex items-center justify-between gap-3 pb-2"
            [class.hidden]="!expanded()"
          >
            <p class="text-sm/6 font-medium text-white">
              You're driving this browser — click and type in it as you normally would.
            </p>
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-2xl px-3.5 py-2 text-sm/6 font-medium text-gray-200 hover:bg-white/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
              (click)="collapse()"
            >
              <ng-icon name="heroArrowsPointingIn" size="16" />
              Exit full screen
            </button>
          </div>

          <!-- Sized from the event's viewport, not a constant here: DCV's
               remoteWidth/remoteHeight must match the browser session or the
               stream crops, and a second copy of 1280x800 in the SPA is a copy
               that will drift. Inline it keeps that aspect ratio; expanded it
               fills the pane and letterboxes inside, so the remote display is
               never scaled to a shape it is not rendering. -->
          <div
            class="relative overflow-hidden bg-black"
            [class]="
              expanded()
                ? 'min-h-0 flex-1 rounded-xl'
                : 'rounded-xl ring-1 ring-gray-900/10 dark:ring-white/10'
            "
            [style.aspectRatio]="expanded() ? null : aspectRatio()"
          >
            <iframe
              #viewerFrame
              class="h-full w-full border-0"
              title="Browser sign-in"
              [src]="src"
              (load)="onFrameLoad()"
            ></iframe>

            @if (!expanded()) {
              <!-- The inline frame reads as a screenshot, so say plainly that
                   it is live and offer the room to use it. -->
              <button
                type="button"
                class="absolute bottom-3 right-3 inline-flex items-center gap-2 rounded-2xl bg-gray-900/80 px-3.5 py-2 text-sm/6 font-medium text-white ring-1 ring-white/20 backdrop-blur-sm hover:bg-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                (click)="expand()"
              >
                <ng-icon name="heroArrowsPointingOut" size="16" />
                Take over full screen
              </button>
            }
          </div>
        </div>
      }
    </section>
  `,
})
export class BrowserLoginPromptComponent {
  readonly request = input.required<BrowserLoginRequest>();

  private readonly service = inject(BrowserLoginService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly open = signal(false);
  protected readonly expanded = signal(false);
  protected readonly minting = signal(false);
  protected readonly error = signal<string | null>(null);

  /** Ticks so the deadline countdown re-evaluates without a timer per binding. */
  private readonly now = signal(Date.now());

  /**
   * Queried rather than captured on the iframe's `load` event.
   *
   * The viewer announces itself with a `ready` message as soon as its
   * listener attaches, which can beat `load`. Depending on `load` alone
   * meant dropping that message and leaving the user on a blank frame until
   * the next re-mint.
   */
  private readonly viewerFrame =
    viewChild<ElementRef<HTMLIFrameElement>>('viewerFrame');

  private refreshTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    const tick = setInterval(() => this.now.set(Date.now()), 1000);

    // The viewer announces itself once its listener is attached. Accepting it
    // only from the sandbox origin keeps any other frame on the page from
    // provoking us into posting a live signed URL.
    const onMessage = (event: MessageEvent) => {
      const origin = this.request().sandboxOrigin;
      if (!origin || event.origin !== origin) return;
      const data = event.data as { type?: unknown } | null;
      if (!data || data.type !== 'browser-live-view/ready') return;
      this.postToFrame();
    };
    window.addEventListener('message', onMessage);

    this.destroyRef.onDestroy(() => {
      clearInterval(tick);
      window.removeEventListener('message', onMessage);
      this.clearRefresh();
    });

    // Stop offering the viewer the moment the window closes. The browser is
    // already released by then, so a stream would be about to disappear.
    effect(() => {
      if (this.lapsed() && this.open()) {
        this.open.set(false);
        this.clearRefresh();
      }
    });
  }

  protected readonly lapsed = computed(() =>
    this.service.hasLapsed(this.request(), this.now()),
  );

  /**
   * Leave full screen the moment the window closes.
   *
   * Without this, a deadline that passes while the user is expanded strands a
   * full-viewport overlay over a stream that has already gone — covering the
   * conversation with a dead black rectangle whose only exit is one button.
   */
  private readonly collapseOnLapse = effect(() => {
    if (this.lapsed()) this.expanded.set(false);
  });

  protected readonly canFrame = computed(() => !!this.request().sandboxOrigin);

  protected readonly aspectRatio = computed(() => {
    const { width, height } = this.request().viewport;
    return height > 0 ? `${width} / ${height}` : '16 / 10';
  });

  protected readonly remainingLabel = computed<string | null>(() => {
    const deadline = this.request().deadlineAt;
    if (!deadline) return null;
    const ms = Date.parse(deadline) - this.now();
    if (!Number.isFinite(ms) || ms <= 0) return null;
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
  });

  /** Origin of the minted live-view URL, for the frame's `connect-src`. */
  private readonly streamOrigin = signal<string | null>(null);

  /**
   * The viewer page, with the CSP it needs declared in the query.
   *
   * The sandbox origin's CloudFront function composes `connect-src` from this,
   * which is what allows DCV's WebSocket through. Without it the page loads and
   * the stream silently never connects — no error, just black.
   *
   * Derived from the **minted URL's own origin** rather than a wildcard like
   * `https://*.amazonaws.com`: we always mint before showing the frame, so the
   * exact endpoint is known by then, and naming it keeps the grant as narrow as
   * the thing it is for. Returns null until then, which is why the frame is
   * gated on `open()`.
   */
  protected readonly frameSrc = computed<SafeResourceUrl | null>(() => {
    const origin = this.request().sandboxOrigin;
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

  protected async openViewer(): Promise<void> {
    // Re-entry guard. Without it a second activation of the button mints a
    // second live-view URL ~30ms after the first: two `POST .../live-view`
    // for one open, measured on dev. Each mint is a live SigV4-signed
    // credential, and the second one drove a second `dcv.authenticate` that
    // failed while the first was still connecting.
    if (this.minting() || this.open()) return;

    this.error.set(null);
    this.minting.set(true);
    try {
      // Mint before showing the frame so a failure surfaces as a message
      // rather than an empty black box.
      const view = await this.service.mintLiveView(this.request().sessionId);
      this.pending = view;
      try {
        this.streamOrigin.set(new URL(view.url).origin);
      } catch {
        this.error.set('The sign-in session address was not usable.');
        return;
      }
      this.open.set(true);
      // The frame may not exist yet on this tick, so all three of this call,
      // the iframe's `load` and the viewer's own `ready` re-drive postToFrame
      // — whichever arrives first is the one that counts. They are NOT
      // no-ops: every one of them posts. The viewer is idempotent on the
      // connection ATTEMPT for exactly this reason; without that it opened a
      // second DCV socket that closed the first.
      this.postToFrame();
      this.scheduleRefresh(view.expiresAt);
    } catch {
      this.error.set(
        'Could not open the sign-in viewer. The browser session may have ended.',
      );
    } finally {
      this.minting.set(false);
    }
  }

  private pending: { url: string; viewport: { width: number; height: number } } | null =
    null;

  protected onFrameLoad(): void {
    this.postToFrame();
  }

  protected expand(): void {
    this.expanded.set(true);
  }

  protected collapse(): void {
    this.expanded.set(false);
  }

  /**
   * Hand the freshly minted URL to the viewer.
   *
   * Targeted at the sandbox origin explicitly rather than `'*'`: that origin
   * also serves untrusted MCP App HTML, and a wildcard target would post a
   * live signed URL to whatever happened to be in the frame.
   */
  private postToFrame(): void {
    const origin = this.request().sandboxOrigin;
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
    this.refreshTimer = setTimeout(() => {
      if (!this.open() || this.lapsed()) return;
      void this.openViewerRefresh();
    }, delay);
  }

  private async openViewerRefresh(): Promise<void> {
    try {
      const view = await this.service.mintLiveView(this.request().sessionId);
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

  protected async finish(): Promise<void> {
    this.clearRefresh();
    this.collapse();
    this.open.set(false);
    await this.service.complete(this.request().interruptId);
  }

  protected async dismiss(): Promise<void> {
    this.clearRefresh();
    this.collapse();
    this.open.set(false);
    await this.service.skip(this.request().interruptId);
  }
}
