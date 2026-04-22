import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import {
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
 * Catalog lives on app-api (`GET /connectors`). Consent initiation lives
 * on inference-api (`POST /connectors/{id}/initiate-consent`) because
 * AgentCore's IdentityClient needs the workload token that the runtime
 * middleware injects only on the inference path.
 */
@Injectable({ providedIn: 'root' })
export class UserConnectorsService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);
  private readonly config = inject(ConfigService);

  private readonly appApiUrl = computed(() => `${this.config.appApiUrl()}/connectors`);
  private readonly inferenceUrl = computed(
    () => `${this.config.inferenceApiUrl()}/connectors`,
  );

  readonly connectorsResource = resource<UserConnector[], void>({
    loader: async () => {
      await this.auth.ensureAuthenticated();
      const response = await firstValueFrom(
        this.http.get<{ connectors: Record<string, unknown>[] }>(`${this.appApiUrl()}/`),
      );
      return response.connectors.map((c) => toCamelCase<UserConnector>(c));
    },
  });

  async initiateConsent(providerId: string): Promise<InitiateConsentResponse> {
    await this.auth.ensureAuthenticated();
    // AgentCore's GetResourceOauth2Token requires a callback URL. The runtime
    // reads this header in prod via AgentCoreContextMiddleware; the settings
    // page bypasses the runtime, so we set it explicitly on the request.
    // We also tack the provider_id onto the callback URL as a query param so
    // /oauth-complete can surface the right providerId in its postMessage.
    const callback = new URL('/oauth-complete', window.location.origin);
    callback.searchParams.set('provider_id', providerId);
    const raw = await firstValueFrom(
      this.http.post<Record<string, unknown>>(
        `${this.inferenceUrl()}/${providerId}/initiate-consent`,
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

  reload(): void {
    this.connectorsResource.reload();
  }
}
