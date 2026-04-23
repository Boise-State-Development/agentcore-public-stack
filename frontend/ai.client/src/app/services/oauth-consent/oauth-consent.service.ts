import { Injectable, signal, computed, inject, DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { fromEvent } from 'rxjs';

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
  authorizationUrl: string;
  interruptId?: string;
  receivedAt: number;
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
 * tokens are now available; the handler is expected to POST a resume
 * request to `/invocations` with `interrupt_responses` populated.
 */
export type OAuthResumeHandler = (interruptIds: string[]) => void | Promise<void>;

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

  /** Map of providerId → request. A provider only appears once, even if
   *  the backend emits duplicates mid-stream. */
  private readonly requests = signal<Map<string, OAuthConsentRequest>>(new Map());

  /** ProviderIds whose popup is currently open. */
  private readonly inFlight = signal<Set<string>>(new Set());

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
  requestConsent(providerId: string, authorizationUrl: string, interruptId?: string): void {
    if (!isSafeConsentUrl(authorizationUrl)) {
      console.error(
        'OAuth consent rejected: authorizationUrl is not https',
        { providerId },
      );
      return;
    }
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(providerId, {
        providerId,
        authorizationUrl,
        interruptId,
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
  openConsentPopup(providerId: string): boolean {
    const request = this.requests().get(providerId);
    if (!request) {
      return false;
    }

    // Re-validate on the hot path even though requestConsent already
    // checked — defensive against anyone mutating the stored entry.
    if (!isSafeConsentUrl(request.authorizationUrl)) {
      console.error(
        'OAuth consent rejected at open: authorizationUrl is not https',
        { providerId },
      );
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

    const popup = window.open(request.authorizationUrl, `oauth-${providerId}`, features);

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
    return true;
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
    return request ? request.authorizationUrl : null;
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
   * Clear a single consent request — called from the UI after the user
   * completes or dismisses a provider, or when the chat is reset.
   */
  dismiss(providerId: string): void {
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
  }

  /** Reset all state (new session, logout). */
  clear(): void {
    this.requests.set(new Map());
    this.inFlight.set(new Set());
    this.blocked.set(new Set());
    this.lastCompletion.set(null);
  }

  /** Acknowledge the last completion signal after the UI has reacted. */
  acknowledgeCompletion(): void {
    this.lastCompletion.set(null);
  }

  private handleCompletion(message: OAuthCompleteMessage): void {
    this.lastCompletion.set(message);
    if (message.status !== 'success' || !message.providerId) {
      return;
    }

    // Capture the paused interrupt id BEFORE dismissing the request, since
    // dismiss removes the entry the handler needs. A user-initiated
    // settings-page consent has no interruptId — nothing to resume.
    const request = this.requests().get(message.providerId);
    this.dismiss(message.providerId);

    if (!request?.interruptId || !this.resumeHandler) {
      return;
    }

    void Promise.resolve(this.resumeHandler([request.interruptId])).catch((err) => {
      // Resume failures are surfaced through the resume request's own error
      // handling — log here for diagnostics but don't crash the consent flow.
      console.error('OAuth resume handler failed', err);
    });
  }
}
