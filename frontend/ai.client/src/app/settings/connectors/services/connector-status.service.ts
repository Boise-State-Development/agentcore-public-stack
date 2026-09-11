import { Injectable, effect, inject, signal } from '@angular/core';
import { UserConnectorsService } from './user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';

/** What we know about a provider's connection right now. */
export type ConnectionState = 'unknown' | 'connected' | 'disconnected';

/**
 * Cached, side-effect-free connection state per OAuth provider.
 *
 * The tools picker needs to answer "will this tool actually work?" for every
 * OAuth-gated tool at once — 13 of the 31 tools in prod. Three things make a
 * cache the right shape rather than a per-row fetch:
 *
 * * `initiateConsent` is the wrong call for a badge: it records a pending
 *   session server-side every time it hands back a URL. `getStatus` exists
 *   precisely because a badge must not commit the user to a consent flow.
 * * Several tools share one provider (Gmail, Calendar, Drive and Tasks are
 *   four rows and one `google-*` connection each), so requests are keyed by
 *   provider, not by tool.
 * * A failed probe is deliberately recorded as `unknown`, not `disconnected`.
 *   Telling someone they are disconnected because a status call timed out
 *   sends them into a consent flow they do not need.
 */
@Injectable({ providedIn: 'root' })
export class ConnectorStatusService {
  private readonly connectors = inject(UserConnectorsService);
  private readonly consent = inject(OAuthConsentService);

  private readonly states = signal<Record<string, ConnectionState>>({});
  /** Providers with a probe in flight — prevents duplicate concurrent fetches. */
  private readonly inFlight = new Set<string>();

  readonly statuses = this.states.asReadonly();

  constructor() {
    // A completed consent invalidates what we cached. Without this the chip
    // still reads "Connect" after the user has just connected, which looks
    // like the consent silently failed.
    effect(() => {
      const completion = this.consent.completion();
      if (completion?.status === 'success' && completion.providerId) {
        this.markConnected(completion.providerId);
      }
    });
  }

  stateFor(providerId: string | null | undefined): ConnectionState {
    if (!providerId) return 'unknown';
    return this.states()[providerId] ?? 'unknown';
  }

  /** Optimistic flip after a consent completes, so the UI reacts immediately. */
  markConnected(providerId: string): void {
    this.states.update((current) => ({ ...current, [providerId]: 'connected' }));
  }

  /** Drop a provider's cached state so the next `ensure` re-probes it. */
  invalidate(providerId: string): void {
    this.states.update((current) => {
      if (!(providerId in current)) return current;
      const next = { ...current };
      delete next[providerId];
      return next;
    });
  }

  /**
   * Probe any provider we have no state for yet. Already-known providers are
   * left alone — this runs whenever the picker opens, and re-probing a dozen
   * providers on every open would be a burst of requests for an answer that
   * rarely changes within a session.
   */
  async ensure(providerIds: readonly (string | null | undefined)[]): Promise<void> {
    const pending = [
      ...new Set(
        providerIds.filter(
          (id): id is string =>
            !!id && !(id in this.states()) && !this.inFlight.has(id),
        ),
      ),
    ];
    if (pending.length === 0) return;

    for (const id of pending) this.inFlight.add(id);

    await Promise.all(
      pending.map(async (id) => {
        try {
          const response = await this.connectors.getStatus(id);
          this.states.update((current) => ({
            ...current,
            [id]: response.connected ? 'connected' : 'disconnected',
          }));
        } catch {
          // Left as `unknown` on purpose — see the class comment.
          this.states.update((current) => ({ ...current, [id]: 'unknown' }));
        } finally {
          this.inFlight.delete(id);
        }
      }),
    );
  }
}
