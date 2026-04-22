import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import {
  OAuthConnector,
  OAuthConnectorListResponse,
  OAuthProvider,
  OAuthProviderListResponse,
} from '../models';

function toCamelCase(obj: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};
  for (const [key, value] of Object.entries(obj)) {
    const camelKey = key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
    result[camelKey] = value;
  }
  return result;
}

/**
 * Service for managing user OAuth connectors.
 *
 * Provides access to available providers and user's connectors,
 * as well as connect/disconnect operations.
 */
@Injectable({
  providedIn: 'root'
})
export class ConnectorsService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/oauth`);

  readonly connectorsResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchConnectors();
    }
  });

  readonly providersResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchProviders();
    }
  });

  getConnectors(): OAuthConnector[] {
    return this.connectorsResource.value()?.connectors ?? [];
  }

  getProviders(): OAuthProvider[] {
    return this.providersResource.value()?.providers ?? [];
  }

  getConnectorByProviderId(providerId: string): OAuthConnector | undefined {
    return this.getConnectors().find(c => c.providerId === providerId);
  }

  async fetchConnectors(): Promise<OAuthConnectorListResponse> {
    // Backend endpoint still returns a `connections` array — translate at the service layer.
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl()}/connections`)
    );
    return {
      connectors: response.connections.map((c: any) => toCamelCase(c) as OAuthConnector),
    };
  }

  async fetchProviders(): Promise<OAuthProviderListResponse> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl()}/providers`)
    );
    return {
      providers: response.providers.map((p: any) => toCamelCase(p) as OAuthProvider),
      total: response.total,
    };
  }

  async connect(providerId: string, redirectUrl?: string): Promise<string> {
    const params = redirectUrl ? `?redirect=${encodeURIComponent(redirectUrl)}` : '';
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl()}/connect/${providerId}${params}`)
    );
    return response.authorization_url;
  }

  async disconnect(providerId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/connections/${providerId}`)
    );
    this.connectorsResource.reload();
  }

  reload(): void {
    this.connectorsResource.reload();
    this.providersResource.reload();
  }
}
