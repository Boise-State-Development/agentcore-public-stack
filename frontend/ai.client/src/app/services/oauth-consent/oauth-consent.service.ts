import { Injectable, signal, computed, inject, DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { fromEvent } from 'rxjs';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { SessionService } from '../../session/services/session/session.service';

/**
 * Pending OAuth consent request surfaced by the backend when an external
 * MCP tool needs the user to authorize AgentCore Identity.
 *
 * `interruptId` is set when the request comes from a paused agent turn
 * (SSE `oauth_required` event) so the chat layer can resume the same turn
 * after consent. It's omitted when the user proactively connects from the
 * settings page — in that case there's no agent turn to resume.
 */
export interface OAuthConsentRequest {
  providerId: string;
  /** Authorization URL captured from a live `oauth_required` SSE event.
   *  Absent on requests hydrated from session metadata after a refresh —
   *  AgentCore's URLs expire quickly, so the service re-fetches a fresh one
   *  via `initiate-consent` when the user clicks Connect. */
  authorizationUrl?: string;
  interruptId?: string;
  receivedAt: number;
  /** Id of the assistant message whose tool call triggered this consent request.
   *  Used by the inline message renderer to anchor the prompt to the turn that
   *  needs it. Omitted for proactive consents from the settings page. */
  messageId?: string;
  /** Session id the request belongs to. Required for the backend dismiss
   *  endpoint to clear the persisted breadcrumb so a refresh doesn't
   *  resurrect a dismissed prompt. */
  sessionId?: string;
}

/**
 * postMessage payload shape broadcast by the `/oauth-complete` landing
 * page. Kept in sync with `OAuthCompleteMessage` in
 * `src/app/oauth-complete/oauth-complete.page.ts`.
 */
export interface OAuthCompleteMessage {
  type: 'agentcore-oauth-complete';
  status: 'success' | 'error';
  providerId: string | null;
  error: string | null;
}

/**
 * Handler the chat layer registers to resume a paused agent turn after
 * one or more OAuth consents complete. Receives the interrupt ids whose
 * tokens are now available, plus the originating session id so the
 * handler can resume even when the live ``lastRequestObject`` is gone
 * (post-refresh hydration). The handler is expected to POST a resume
 * request to `/invocations` with `interrupt_responses` populated.
 */
export type OAuthResumeHandler = (
  interruptIds: string[],
  context?: { sessionId?: string },
) => void | Promise<void>;

function isOAuthCompleteMessage(data: unknown): data is OAuthCompleteMessage {
  if (!data || typeof data !== 'object') {
    return false;
  }
  const msg = data as Partial<OAuthCompleteMessage>;
  return msg.type === 'agentcore-oauth-complete';
}

/**
 * Only https URLs are accepted for consent navigation. Guards against a
 * compromised backend or a misconfigured AgentCore response smuggling a
 * `javascript:` or `data:` URL through the `oauth_required` event and
 * executing in our origin when the user clicks Connect.
 */
function isSafeConsentUrl(raw: string): boolean {
  try {
    return new URL(raw).protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Tracks OAuth consent requests surfaced by the SSE stream and coordinates
 * the popup + auto-resume flow.
 *
 * The stream parser calls {@link requestConsent} when an `oauth_required`
 * event arrives; components render a "Connect" affordance bound to
 * {@link pending}. When the user clicks, {@link openConsentPopup} opens the
 * AgentCore Identity URL, and this service listens for the
 * `agentcore-oauth-complete` postMessage from the `/oauth-complete` landing
 * page. On success it dismisses the request and asks the registered
 * {@link OAuthResumeHandler} to fire a resume request — the user does NOT
 * have to retype the original prompt.
 */
@Injectable({ providedIn: 'root' })
export class OAuthConsentService {
  private readonly destroyRef = inject(DestroyRef);
  private readonly userConnectorsService = inject(UserConnectorsService);
  private readonly sessionService = inject(SessionService);

  /** Map of providerId → request. A provider only appears once, even if
   *  the backend emits duplicates mid-stream. */
  private readonly requests = signal<Map<string, OAuthConsentRequest>>(new Map());

  /** Interrupt ids we've already surfaced or resolved this session. Used to
   *  ignore re-emissions of the same `oauth_required` event after a stream
   *  replay or a late server-side breadcrumb clear — without this, a popup
   *  that already completed consent would resurrect once dismissed. New
   *  tool calls always carry a fresh interrupt id (Strands generates it
   *  from `toolUseId`), so legitimate prompts are never suppressed. */
  private readonly seenInterruptIds = new Set<string>();

  /** ProviderIds whose popup is currently open. */
  private readonly inFlight = signal<Set<string>>(new Set());

  /** Public read of inFlight so settings/chat UIs can react when a popup
   *  closes without completing (state needs to flip from "Awaiting" back
   *  to "Connect" so the user can retry). */
  readonly inFlightProviders = this.inFlight.asReadonly();

  /** Active close-watcher intervals keyed by providerId so we can cancel
   *  cleanly on completion / dismissal. */
  private readonly closeWatchers = new Map<string, ReturnType<typeof setInterval>>();

  /** ProviderIds whose popup was blocked on the last open attempt. */
  private readonly blocked = signal<Set<string>>(new Set());

  /** Most recent completion notice surfaced to the chat layer. */
  private readonly lastCompletion = signal<OAuthCompleteMessage | null>(null);

  /** Resume handler registered by the chat layer. Replayed when a
   *  consent completes successfully. */
  private resumeHandler: OAuthResumeHandler | null = null;

  readonly pending = computed<OAuthConsentRequest[]>(() =>
    Array.from(this.requests().values()).sort((a, b) => a.receivedAt - b.receivedAt),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  readonly completion = this.lastCompletion.asReadonly();

  constructor() {
    // Primary channel: BroadcastChannel. AgentCore's OAuth popup navigates
    // through external origins (Google, AgentCore), which triggers Chrome's
    // Cross-Origin-Opener-Policy and severs window.opener. window.postMessage
    // from the /oauth-complete page is silently blocked in that case, so we
    // rely on a same-origin BroadcastChannel to bridge popup → opener.
    try {
      const channel = new BroadcastChannel('agentcore-oauth-complete');
      channel.addEventListener('message', (event) => {
        if (!isOAuthCompleteMessage(event.data)) {
          return;
        }
        this.handleCompletion(event.data);
      });
      this.destroyRef.onDestroy(() => channel.close());
    } catch {
      // BroadcastChannel unavailable — fall back to postMessage below.
    }

    // Fallback channel: window postMessage (pre-COOP browsers, or flows
    // where the popup manages to retain window.opener). The origin guard
    // makes sure cross-origin pages can't spoof a completion.
    fromEvent<MessageEvent>(window, 'message')
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((event) => {
        if (event.origin !== window.location.origin) {
          return;
        }
        if (!isOAuthCompleteMessage(event.data)) {
          return;
        }
        this.handleCompletion(event.data);
      });
  }

  /**
   * Register a consent request coming off the SSE stream.
   * Duplicate providerIds refresh the existing entry — the backend may
   * reissue an interrupt with a new id if the user retried.
   *
   * Rejects non-https URLs — see {@link isSafeConsentUrl}.
   */
  requestConsent(
    providerId: string,
    authorizationUrl: string | undefined,
    interruptId?: string,
    messageId?: string,
    sessionId?: string,
  ): void {
    // Hydration from session metadata passes undefined — the URL gets fetched
    // lazily on Connect. Live SSE flows still pass the URL up front for the
    // fast-path popup with no extra roundtrip.
    if (authorizationUrl !== undefined && !isSafeConsentUrl(authorizationUrl)) {
      console.error(
        'OAuth consent rejected: authorizationUrl is not https',
        { providerId },
      );
      return;
    }
    // Drop re-emissions of an already-handled interrupt. Stream replays after
    // refresh, or a delayed server-side breadcrumb clear, can fire the same
    // `oauth_required` event again — without this guard a successfully
    // consented or dismissed prompt would resurrect.
    if (interruptId && this.seenInterruptIds.has(interruptId)) {
      return;
    }
    if (interruptId) {
      this.seenInterruptIds.add(interruptId);
    }
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(providerId, {
        providerId,
        authorizationUrl,
        interruptId,
        messageId,
        sessionId,
        receivedAt: Date.now(),
      });
      return next;
    });
    // A fresh request clears any prior blocked state for this provider.
    this.blocked.update((set) => {
      if (!set.has(providerId)) {
        return set;
      }
      const next = new Set(set);
      next.delete(providerId);
      return next;
    });
  }

  /**
   * Open the AgentCore Identity consent URL in a popup window.
   *
   * If the browser blocks the popup, we mark the provider as blocked and
   * surface that to the UI rather than navigating the parent tab away —
   * a redirect would tear down the chat mid-conversation and leave the
   * paused agent turn hanging.
   *
   * Returns true if the popup opened, false if it was blocked or the URL
   * failed validation. Callers can use this to trigger a fallback UI.
   */
  async openConsentPopup(providerId: string): Promise<boolean> {
    const request = this.requests().get(providerId);
    if (!request) {
      return false;
    }

    // Hydrated requests don't carry a URL — fetch a fresh one. Stored URLs
    // can also be stale if the live SSE event fired more than a few minutes
    // ago, so we treat any missing/expired URL as a refresh trigger.
    let authorizationUrl = request.authorizationUrl;
    if (!authorizationUrl) {
      this.inFlight.update((set) => {
        const next = new Set(set);
        next.add(providerId);
        return next;
      });
      try {
        const response = await this.userConnectorsService.initiateConsent(providerId);
        if (response.connected || !response.authorizationUrl) {
          // Already consented while paused (e.g. the user authorized in
          // another tab). Drop the request and let the resume handler — if
          // any — fire so the agent can finish the turn.
          this.dismiss(providerId);
          if (request.interruptId && this.resumeHandler) {
            void Promise.resolve(
              this.resumeHandler([request.interruptId], { sessionId: request.sessionId }),
            ).catch((err) =>
              console.error('OAuth resume handler failed after pre-consented refresh', err),
            );
          }
          return false;
        }
        authorizationUrl = response.authorizationUrl;
        this.requests.update((map) => {
          const next = new Map(map);
          const current = next.get(providerId);
          if (current) {
            next.set(providerId, { ...current, authorizationUrl });
          }
          return next;
        });
      } catch (err) {
        console.error('Failed to fetch fresh authorization URL', err);
        this.inFlight.update((set) => {
          if (!set.has(providerId)) return set;
          const next = new Set(set);
          next.delete(providerId);
          return next;
        });
        return false;
      }
    }

    // Re-validate on the hot path even though requestConsent already
    // checked — defensive against anyone mutating the stored entry.
    if (!isSafeConsentUrl(authorizationUrl)) {
      console.error(
        'OAuth consent rejected at open: authorizationUrl is not https',
        { providerId },
      );
      this.inFlight.update((set) => {
        if (!set.has(providerId)) return set;
        const next = new Set(set);
        next.delete(providerId);
        return next;
      });
      return false;
    }

    const width = 520;
    const height = 680;
    const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
    const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);

    const features = [
      `width=${width}`,
      `height=${height}`,
      `left=${Math.round(left)}`,
      `top=${Math.round(top)}`,
      'resizable=yes',
      'scrollbars=yes',
      'status=no',
      'toolbar=no',
      'menubar=no',
      'location=no',
    ].join(',');

    const popup = window.open(authorizationUrl, `oauth-${providerId}`, features);

    if (!popup) {
      this.blocked.update((set) => {
        if (set.has(providerId)) {
          return set;
        }
        const next = new Set(set);
        next.add(providerId);
        return next;
      });
      return false;
    }

    this.blocked.update((set) => {
      if (!set.has(providerId)) {
        return set;
      }
      const next = new Set(set);
      next.delete(providerId);
      return next;
    });

    this.inFlight.update((set) => {
      const next = new Set(set);
      next.add(providerId);
      return next;
    });

    // Watch for the user closing the popup without completing consent.
    // Without this the provider stays "in-flight" forever and the Connect
    // button remains disabled. We poll because there's no reliable
    // cross-browser event for popup close, especially under COOP.
    this.watchPopupClose(providerId, popup);
    return true;
  }

  /** Poll a popup window until it closes; on close, drop the provider out
   *  of `inFlight` so the UI re-enables the Connect button. The pending
   *  request stays so the chat banner can offer a retry. */
  private watchPopupClose(providerId: string, popup: Window): void {
    // Cancel any prior watcher for this provider — only one popup at a time.
    this.cancelCloseWatcher(providerId);

    const interval = setInterval(() => {
      let closed = false;
      try {
        closed = popup.closed;
      } catch {
        // Cross-Origin-Opener-Policy can block reads of `closed` after the
        // popup navigates externally. Give up — the user can dismiss the
        // banner manually if needed.
        this.cancelCloseWatcher(providerId);
        return;
      }
      if (!closed) return;

      this.cancelCloseWatcher(providerId);
      // Only act if still flagged in-flight: a successful completion already
      // ran handleCompletion → dismiss() before the popup's own close.
      if (!this.inFlight().has(providerId)) return;
      this.inFlight.update((set) => {
        if (!set.has(providerId)) return set;
        const next = new Set(set);
        next.delete(providerId);
        return next;
      });
    }, 500);
    this.closeWatchers.set(providerId, interval);
    this.destroyRef.onDestroy(() => this.cancelCloseWatcher(providerId));
  }

  private cancelCloseWatcher(providerId: string): void {
    const interval = this.closeWatchers.get(providerId);
    if (interval !== undefined) {
      clearInterval(interval);
      this.closeWatchers.delete(providerId);
    }
  }

  /** Check whether a popup is still open for this provider. */
  isInFlight(providerId: string): boolean {
    return this.inFlight().has(providerId);
  }

  /** Check whether the last popup-open attempt was blocked. */
  isBlocked(providerId: string): boolean {
    return this.blocked().has(providerId);
  }

  /**
   * Return the https authorization URL for a provider, or null if no
   * pending request. Used by the banner to render an anchor-based fallback
   * when the popup is blocked.
   */
  getAuthorizationUrl(providerId: string): string | null {
    const request = this.requests().get(providerId);
    return request?.authorizationUrl ?? null;
  }

  /**
   * Register the chat-layer handler that resumes the paused agent turn
   * after one or more OAuth consents complete. The handler receives the
   * interrupt ids whose tokens are ready; replacing it (set to null)
   * disables auto-resume.
   */
  setResumeHandler(handler: OAuthResumeHandler | null): void {
    this.resumeHandler = handler;
  }

  /**
   * Drop a single consent request from local state, and (when called from
   * the UI's explicit dismiss button) clear the persisted breadcrumb so a
   * refresh doesn't resurrect the prompt.
   *
   * On completion-driven cleanup ({@link handleCompletion}) we set
   * ``syncServer: false`` because the resume request that follows will
   * remove the same breadcrumb server-side — a separate DELETE would just
   * be redundant network noise.
   */
  dismiss(providerId: string, options?: { syncServer?: boolean }): void {
    const entry = this.requests().get(providerId);
    const sessionId = entry?.sessionId;
    const interruptId = entry?.interruptId;

    this.requests.update((map) => {
      if (!map.has(providerId)) {
        return map;
      }
      const next = new Map(map);
      next.delete(providerId);
      return next;
    });
    this.inFlight.update((set) => {
      if (!set.has(providerId)) {
        return set;
      }
      const next = new Set(set);
      next.delete(providerId);
      return next;
    });
    this.blocked.update((set) => {
      if (!set.has(providerId)) {
        return set;
      }
      const next = new Set(set);
      next.delete(providerId);
      return next;
    });

    if (options?.syncServer === false || !sessionId || !interruptId) {
      return;
    }

    // Best-effort: a backend cleanup failure shouldn't block the UI from
    // hiding the prompt — the prompt is already gone locally.
    void this.sessionService
      .dismissPendingInterrupt(sessionId, interruptId)
      .catch((err) => {
        console.warn('Failed to clear persisted pending_interrupt; local dismiss still applied', err);
      });
  }

  /** Reset all state (new session, logout). */
  clear(): void {
    this.requests.set(new Map());
    this.inFlight.set(new Set());
    this.blocked.set(new Set());
    this.lastCompletion.set(null);
    this.seenInterruptIds.clear();
  }

  /** Acknowledge the last completion signal after the UI has reacted. */
  acknowledgeCompletion(): void {
    this.lastCompletion.set(null);
  }

  private handleCompletion(message: OAuthCompleteMessage): void {
    this.lastCompletion.set(message);
    if (message.providerId) {
      // Completion arrived — the close watcher is no longer needed and
      // would otherwise fire spuriously when the popup auto-closes after
      // postMessage.
      this.cancelCloseWatcher(message.providerId);
    }
    if (message.status !== 'success' || !message.providerId) {
      return;
    }

    // Capture the paused interrupt id BEFORE dismissing the request, since
    // dismiss removes the entry the handler needs. A user-initiated
    // settings-page consent has no interruptId — nothing to resume.
    // Skip server sync: the resume request fired below clears the persisted
    // interrupt server-side, so a separate DELETE would just be redundant.
    const request = this.requests().get(message.providerId);
    this.dismiss(message.providerId, { syncServer: false });

    if (!request?.interruptId || !this.resumeHandler) {
      return;
    }

    void Promise.resolve(
      this.resumeHandler([request.interruptId], { sessionId: request.sessionId }),
    ).catch((err) => {
      // Resume failures are surfaced through the resume request's own error
      // handling — log here for diagnostics but don't crash the consent flow.
      console.error('OAuth resume handler failed', err);
    });
  }
}
