import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroExclamationCircle,
} from '@ng-icons/heroicons/outline';
import { AuthService } from '../auth/auth.service';
import { ConfigService } from '../services/config.service';

/**
 * Landing page for AgentCore Identity's 3-legged OAuth flow.
 *
 * AgentCore Identity redirects the user here after consent completes (or
 * fails). We detect whether this page was opened in a popup:
 *
 *   - Popup: post a message to the opener so the chat can retry the tool
 *     call, then close the window.
 *   - Same tab: show a brief success/error message, then route back to chat.
 *
 * Query params AgentCore Identity may append are not strictly contractual,
 * so we treat missing params as success and known error indicators as
 * failure (`error`, `error_description`). This matches the defensive
 * parsing pattern in `settings/oauth-callback`.
 */

type CompleteState = 'success' | 'error';

/** postMessage payload shape — kept public so other code can type the listener. */
export interface OAuthCompleteMessage {
  type: 'agentcore-oauth-complete';
  status: CompleteState;
  providerId: string | null;
  error: string | null;
}

@Component({
  selector: 'app-oauth-complete',
  imports: [NgIcon],
  providers: [
    provideIcons({ heroCheckCircle, heroExclamationCircle }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <main class="page">
      @if (state() === 'success') {
        <section class="card success" role="status" aria-live="polite">
          <ng-icon name="heroCheckCircle" class="icon success-icon" aria-hidden="true" />
          <h1 class="title">Connected</h1>
          <p class="subtitle">
            @if (providerLabel()) {
              {{ providerLabel() }} is now linked to your account.
            } @else {
              Authorization complete.
            }
          </p>
          <p class="hint">{{ dismissHint() }}</p>
        </section>
      } @else {
        <section class="card error" role="alert" aria-live="assertive">
          <ng-icon name="heroExclamationCircle" class="icon error-icon" aria-hidden="true" />
          <h1 class="title">Connection failed</h1>
          <p class="subtitle">{{ errorMessage() }}</p>
          <p class="hint">{{ dismissHint() }}</p>
        </section>
      }
    </main>
  `,
  styles: `
    :host {
      display: block;
      min-height: 100dvh;
      background: var(--color-gray-50);
    }

    :host-context(html.dark) {
      background: var(--color-gray-900);
    }

    .page {
      min-height: 100dvh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem;
    }

    .card {
      width: 100%;
      max-width: 28rem;
      padding: 2rem;
      border-radius: 0.75rem;
      background: var(--color-white);
      border: 1px solid var(--color-gray-200);
      box-shadow: 0 1px 2px rgb(0 0 0 / 0.05);
      text-align: center;
    }

    :host-context(html.dark) .card {
      background: var(--color-gray-800);
      border-color: var(--color-gray-700);
    }

    .icon {
      display: inline-flex;
      width: 3rem;
      height: 3rem;
      margin-bottom: 1rem;
    }

    .success-icon {
      color: var(--color-green-600);
    }

    :host-context(html.dark) .success-icon {
      color: var(--color-green-400);
    }

    .error-icon {
      color: var(--color-red-600);
    }

    :host-context(html.dark) .error-icon {
      color: var(--color-red-400);
    }

    .title {
      font-weight: 600;
      font-size: 1.5rem;
      margin: 0 0 0.5rem;
      color: var(--color-gray-900);
    }

    :host-context(html.dark) .title {
      color: var(--color-gray-100);
    }

    .subtitle {
      margin: 0 0 1rem;
      color: var(--color-gray-700);
      font-size: 0.95rem;
    }

    :host-context(html.dark) .subtitle {
      color: var(--color-gray-300);
    }

    .hint {
      margin: 0;
      font-size: 0.8125rem;
      color: var(--color-gray-500);
    }
  `,
})
export class OAuthCompletePage implements OnInit, OnDestroy {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly authService = inject(AuthService);
  private readonly config = inject(ConfigService);

  private redirectTimer: ReturnType<typeof setTimeout> | null = null;

  readonly state = signal<CompleteState>('success');
  readonly providerId = signal<string | null>(null);
  readonly sessionUri = signal<string | null>(null);
  readonly errorMessage = signal<string>('Authorization was denied or did not complete.');
  private readonly isPopup = signal<boolean>(false);
  private readonly finalizing = signal<boolean>(false);

  readonly providerLabel = computed(() => {
    const id = this.providerId();
    if (!id) {
      return null;
    }
    return id
      .replace(/[-_]+/g, ' ')
      .split(' ')
      .filter((part) => part.length > 0)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  });

  readonly dismissHint = computed(() => {
    if (!this.isPopup()) {
      return 'Redirecting back to your chat…';
    }
    return this.state() === 'error'
      ? 'You can close this window once you\'re done reading the error.'
      : 'You can close this window.';
  });

  ngOnInit(): void {
    const params = this.route.snapshot.queryParamMap;
    const error = params.get('error');
    const errorDescription = params.get('error_description');
    // AgentCore echoes our custom_state back as `state`; we set it server-side
    // to the providerId when initiating consent from the settings page.
    const providerId =
      params.get('provider_id') ??
      params.get('providerId') ??
      params.get('state');

    // AgentCore's redirect also carries `session_id`, which is the
    // `request_uri` from the initial authorize call. We must hand it back
    // to CompleteResourceTokenAuth or the token vault stays empty.
    const sessionUri = params.get('session_id') ?? params.get('sessionUri');

    if (providerId) {
      this.providerId.set(providerId);
    }
    if (sessionUri) {
      this.sessionUri.set(sessionUri);
    }

    if (error) {
      this.state.set('error');
      this.errorMessage.set(
        errorDescription?.trim() || this.describeError(error),
      );
    }

    const inPopup = this.detectPopup();
    this.isPopup.set(inPopup);

    // Finalize AgentCore's OAuth session (exchanges the `request_uri` for a
    // persisted token). Must happen BEFORE we tell the opener we're done —
    // otherwise the opener's next tool call will still see "consent required".
    if (this.state() === 'success' && sessionUri) {
      this.finalizing.set(true);
      this.finalizeConsent(sessionUri, providerId)
        .catch((err) => {
          this.state.set('error');
          this.errorMessage.set(
            err instanceof Error
              ? `Couldn't finalize authorization: ${err.message}`
              : "Couldn't finalize authorization.",
          );
        })
        .finally(() => {
          this.finalizing.set(false);
          if (inPopup) {
            this.notifyOpenerAndClose();
          } else {
            this.redirectTimer = setTimeout(() => this.router.navigate(['/']), 2000);
          }
        });
      return;
    }

    if (inPopup) {
      this.notifyOpenerAndClose();
    } else {
      this.redirectTimer = setTimeout(() => this.router.navigate(['/']), 2000);
    }
  }

  private async finalizeConsent(
    sessionUri: string,
    providerId: string | null,
  ): Promise<void> {
    const baseUrl = this.config.appApiUrl();
    if (!baseUrl) {
      throw new Error('appApiUrl not configured');
    }
    const token = this.authService.getAccessToken();
    if (!token) {
      throw new Error('No access token available');
    }
    const url = `${baseUrl}/connectors/complete-consent`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        session_uri: sessionUri,
        provider_id: providerId,
      }),
    });
    if (!response.ok) {
      const text = await response.text().catch(() => '');
      throw new Error(`HTTP ${response.status}: ${text || response.statusText}`);
    }
  }

  ngOnDestroy(): void {
    if (this.redirectTimer !== null) {
      clearTimeout(this.redirectTimer);
    }
  }

  /**
   * A page is "in a popup" only when it has an opener from the same origin
   * that it can actually postMessage to. We guard the property reads
   * defensively because cross-origin `window.opener` access can throw.
   */
  private detectPopup(): boolean {
    try {
      return typeof window !== 'undefined' && window.opener != null && window.opener !== window;
    } catch {
      return false;
    }
  }

  private notifyOpenerAndClose(): void {
    const message: OAuthCompleteMessage = {
      type: 'agentcore-oauth-complete',
      status: this.state(),
      providerId: this.providerId(),
      error: this.state() === 'error' ? this.errorMessage() : null,
    };

    // Primary channel: BroadcastChannel. Survives the Cross-Origin-Opener-Policy
    // split that severs window.opener once a popup navigates through external
    // origins (AgentCore, Google) and back. Same-origin tabs sharing a channel
    // name always see each other, regardless of opener relationship.
    try {
      const channel = new BroadcastChannel('agentcore-oauth-complete');
      channel.postMessage(message);
      // Give the message a tick to propagate before closing the channel.
      setTimeout(() => channel.close(), 200);
    } catch {
      // BroadcastChannel unavailable — fall back to postMessage below.
    }

    // Fallback: postMessage to opener. Works when COOP isn't in play.
    try {
      window.opener?.postMessage(message, window.location.origin);
    } catch {
      // Cross-origin or COOP-isolated opener — BroadcastChannel above
      // handles the handoff in that case.
    }

    // Only auto-close on success. On error, leave the window open so the
    // user can read the failure reason — they dismiss it manually.
    if (this.state() !== 'success') {
      return;
    }

    this.redirectTimer = setTimeout(() => {
      try {
        window.close();
      } catch {
        // Some browsers refuse to close pages they didn't open; show the
        // static "you can close this window" hint and let the user dismiss.
      }
    }, 400);
  }

  private describeError(code: string): string {
    switch (code) {
      case 'access_denied':
        return 'You declined the authorization request.';
      case 'invalid_scope':
        return 'The requested permissions are not available for this account.';
      case 'server_error':
        return 'The provider could not complete the request. Try again in a moment.';
      default:
        return 'Authorization did not complete. Try again.';
    }
  }
}
