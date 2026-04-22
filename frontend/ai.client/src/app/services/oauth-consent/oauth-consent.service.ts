import { Injectable, signal, computed, inject, DestroyRef } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { fromEvent } from 'rxjs';
import { filter } from 'rxjs/operators';

/**
 * Pending OAuth consent request surfaced by the backend when an external
 * MCP tool needs the user to authorize AgentCore Identity.
 */
export interface OAuthConsentRequest {
  providerId: string;
  authorizationUrl: string;
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

function isOAuthCompleteMessage(data: unknown): data is OAuthCompleteMessage {
  if (!data || typeof data !== 'object') {
    return false;
  }
  const msg = data as Partial<OAuthCompleteMessage>;
  return msg.type === 'agentcore-oauth-complete';
}

/**
 * Tracks OAuth consent requests surfaced by the SSE stream and coordinates
 * the popup flow.
 *
 * The stream parser calls {@link requestConsent} when an `oauth_required`
 * event arrives; components render a "Connect" affordance bound to
 * {@link pending}. When the user clicks, {@link openConsentPopup} opens the
 * AgentCore Identity URL, and this service listens for the
 * `agentcore-oauth-complete` postMessage from the `/oauth-complete` landing
 * page to resolve the provider.
 */
@Injectable({ providedIn: 'root' })
export class OAuthConsentService {
  private readonly destroyRef = inject(DestroyRef);

  /** Map of providerId → request. A provider only appears once, even if
   *  the backend emits duplicates mid-stream. */
  private readonly requests = signal<Map<string, OAuthConsentRequest>>(new Map());

  /** ProviderIds whose popup is currently open. */
  private readonly inFlight = signal<Set<string>>(new Set());

  /** Most recent completion notice surfaced to the chat layer. */
  private readonly lastCompletion = signal<OAuthCompleteMessage | null>(null);

  readonly pending = computed<OAuthConsentRequest[]>(() =>
    Array.from(this.requests().values()).sort((a, b) => a.receivedAt - b.receivedAt),
  );

  readonly hasPending = computed<boolean>(() => this.requests().size > 0);

  readonly completion = this.lastCompletion.asReadonly();

  constructor() {
    // Listen for postMessages from the /oauth-complete landing page. The
    // origin guard makes sure cross-origin pages can't spoof a completion.
    fromEvent<MessageEvent>(window, 'message')
      .pipe(
        filter((event) => event.origin === window.location.origin),
        filter((event) => isOAuthCompleteMessage(event.data)),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((event) => {
        const message = event.data as OAuthCompleteMessage;
        this.handleCompletion(message);
      });
  }

  /**
   * Register a consent request coming off the SSE stream.
   * Duplicate providerIds refresh the existing entry (URLs can rotate).
   */
  requestConsent(providerId: string, authorizationUrl: string): void {
    this.requests.update((map) => {
      const next = new Map(map);
      next.set(providerId, {
        providerId,
        authorizationUrl,
        receivedAt: Date.now(),
      });
      return next;
    });
  }

  /**
   * Open the AgentCore Identity consent URL in a popup window.
   * Falls back to a same-tab redirect if the popup is blocked.
   */
  openConsentPopup(providerId: string): void {
    const request = this.requests().get(providerId);
    if (!request) {
      return;
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
      // Popup blocked — fall back to opening in the current tab. The
      // `/oauth-complete` page will route back to `/` once consent resolves.
      window.location.href = request.authorizationUrl;
      return;
    }

    this.inFlight.update((set) => {
      const next = new Set(set);
      next.add(providerId);
      return next;
    });
  }

  /** Check whether a popup is still open for this provider. */
  isInFlight(providerId: string): boolean {
    return this.inFlight().has(providerId);
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
  }

  /** Reset all state (new session, logout). */
  clear(): void {
    this.requests.set(new Map());
    this.inFlight.set(new Set());
    this.lastCompletion.set(null);
  }

  /** Acknowledge the last completion signal after the UI has reacted. */
  acknowledgeCompletion(): void {
    this.lastCompletion.set(null);
  }

  private handleCompletion(message: OAuthCompleteMessage): void {
    this.lastCompletion.set(message);
    if (message.status === 'success' && message.providerId) {
      this.dismiss(message.providerId);
    }
  }
}
