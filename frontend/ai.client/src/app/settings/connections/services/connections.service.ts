import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import {
  OAuthConnection,
  OAuthConnectionListResponse,
  OAuthProvider,
  OAuthProviderListResponse,
  OAuthConnectResponse,
} from '../models';

/**
 * Convert snake_case to camelCase for frontend models.
 */
function toCamelCase(obj: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};
  for (const [key, value] of Object.entries(obj)) {
    const camelKey = key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
    result[camelKey] = value;
  }
  return result;
}

/**
 * Service for managing user OAuth connections.
 *
 * Provides access to available providers and user's connections,
 * as well as connect/disconnect operations.
 */
@Injectable({
  providedIn: 'root'
})
export class ConnectionsService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  private readonly baseUrl = `${environment.appApiUrl}/oauth`;

  /**
   * Reactive resource for fetching user's OAuth connections.
   */
  readonly connectionsResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchConnections();
    }
  });

  /**
   * Reactive resource for fetching available OAuth providers.
   */
  readonly providersResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchProviders();
    }
  });

  /**
   * Get all user connections (from resource).
   */
  getConnections(): OAuthConnection[] {
    return this.connectionsResource.value()?.connections ?? [];
  }

  /**
   * Get all available providers (from resource).
   */
  getProviders(): OAuthProvider[] {
    return this.providersResource.value()?.providers ?? [];
  }

  /**
   * Get a connection by provider ID.
   */
  getConnectionByProviderId(providerId: string): OAuthConnection | undefined {
    return this.getConnections().find(c => c.providerId === providerId);
  }

  /**
   * Fetch user's OAuth connections from the API.
   */
  async fetchConnections(): Promise<OAuthConnectionListResponse> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl}/connections`)
    );
    return {
      connections: response.connections.map((c: any) => toCamelCase(c) as OAuthConnection),
    };
  }

  /**
   * Fetch available OAuth providers from the API.
   */
  async fetchProviders(): Promise<OAuthProviderListResponse> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl}/providers`)
    );
    return {
      providers: response.providers.map((p: any) => toCamelCase(p) as OAuthProvider),
      total: response.total,
    };
  }

  /**
   * Initiate OAuth connection flow.
   * Returns the authorization URL to redirect to.
   */
  async connect(providerId: string, redirectUrl?: string): Promise<string> {
    const params = redirectUrl ? `?redirect=${encodeURIComponent(redirectUrl)}` : '';
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl}/connect/${providerId}${params}`)
    );
    return response.authorization_url;
  }

  /**
   * Disconnect from an OAuth provider.
   */
  async disconnect(providerId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl}/connections/${providerId}`)
    );
    this.connectionsResource.reload();
  }

  /**
   * Reload both resources.
   */
  reload(): void {
    this.connectionsResource.reload();
    this.providersResource.reload();
  }
}
