import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import {
  ConnectorStatusResponse,
  InitiateConsentResponse,
  UserConnector,
} from '../models/user-connector.model';

function toCamelCase<T>(obj: Record<string, unknown>): T {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    const camel = k.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());
    out[camel] = v;
  }
  return out as T;
}

/**
 * User-facing connectors service.
 *
 * All routes live on app-api. The AgentCore runtime data plane only proxies
 * `/invocations` and `/ping`, so the previous inference-api consent endpoints
 * were unreachable in cloud — app-api mints workload context via the
 * `AGENTCORE_RUNTIME_WORKLOAD_NAME` fallback in `agentcore_identity.py`.
 */
@Injectable({ providedIn: 'root' })
export class UserConnectorsService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);
  private readonly config = inject(ConfigService);

  private readonly appApiUrl = computed(() => `${this.config.appApiUrl()}/connectors`);

  readonly connectorsResource = resource<UserConnector[], void>({
    loader: async () => {
      await this.auth.ensureAuthenticated();
      const response = await firstValueFrom(
        this.http.get<{ connectors: Record<string, unknown>[] }>(`${this.appApiUrl()}/`),
      );
      return response.connectors.map((c) => toCamelCase<UserConnector>(c));
    },
  });

  /**
   * Side-effect-free check of whether AgentCore's vault has a usable token
   * for this user + provider. Use it to render a "Connected" badge without
   * committing the user to a consent flow — `initiateConsent` records a
   * server-side pending session every time it returns an auth URL, which is
   * wasteful when we only want a badge.
   *
   * Sends `OAuth2CallbackUrl` for the same reason `initiateConsent` does:
   * the runtime injects it in prod, but the settings page bypasses the
   * runtime, so without it the backend's identity client has no callback
   * URL to give AgentCore Identity and rejects the request 503.
   */
  async getStatus(providerId: string): Promise<ConnectorStatusResponse> {
    await this.auth.ensureAuthenticated();
    const callback = new URL('/oauth-complete', window.location.origin);
    return await firstValueFrom(
      this.http.get<ConnectorStatusResponse>(
        `${this.appApiUrl()}/${providerId}/status`,
        {
          headers: {
            OAuth2CallbackUrl: callback.toString(),
          },
        },
      ),
    );
  }

  async initiateConsent(providerId: string): Promise<InitiateConsentResponse> {
    await this.auth.ensureAuthenticated();
    // AgentCore's GetResourceOauth2Token requires a callback URL. App-api
    // reads this header via the shared AgentCoreContextMiddleware. The
    // backend's `_resolve_callback_url` re-appends `provider_id` itself,
    // and AgentCoreContextMiddleware rejects any URL with a query string
    // as a redirect-pivot guard — so we send a bare `/oauth-complete`.
    const callback = new URL('/oauth-complete', window.location.origin);
    const raw = await firstValueFrom(
      this.http.post<Record<string, unknown>>(
        `${this.appApiUrl()}/${providerId}/initiate-consent`,
        {},
        {
          headers: {
            OAuth2CallbackUrl: callback.toString(),
          },
        },
      ),
    );
    return toCamelCase<InitiateConsentResponse>(raw);
  }

  /**
   * Best-effort disconnect: persists the disconnect intent so the next tool
   * call forces a fresh consent flow. AgentCore Identity exposes no per-user
   * vault-delete, so the upstream token at the provider keeps existing
   * until it expires or the user revokes our app from their provider account.
   */
  async disconnect(providerId: string): Promise<void> {
    await this.auth.ensureAuthenticated();
    await firstValueFrom(
      this.http.delete(`${this.appApiUrl()}/${providerId}/connection`),
    );
  }

  reload(): void {
    this.connectorsResource.reload();
  }
}
