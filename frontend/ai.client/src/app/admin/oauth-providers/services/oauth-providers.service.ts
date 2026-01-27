import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import {
  OAuthProvider,
  OAuthProviderListResponse,
  OAuthProviderCreateRequest,
  OAuthProviderUpdateRequest,
} from '../models/oauth-provider.model';

/**
 * Convert camelCase to snake_case for backend API.
 */
function toSnakeCase(obj: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value === undefined) continue;
    const snakeKey = key.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`);
    result[snakeKey] = value;
  }
  return result;
}

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
 * Service to manage OAuth Providers.
 *
 * Provides access to the provider list for use in forms and displays,
 * as well as CRUD operations for provider management.
 */
@Injectable({
  providedIn: 'root'
})
export class OAuthProvidersService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  private readonly baseUrl = `${environment.appApiUrl}/admin/oauth-providers`;

  /**
   * Reactive resource for fetching OAuth Providers.
   */
  readonly providersResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchProviders();
    }
  });

  /**
   * Get all OAuth Providers (from resource).
   */
  getProviders(): OAuthProvider[] {
    return this.providersResource.value()?.providers ?? [];
  }

  /**
   * Get only enabled OAuth Providers.
   */
  getEnabledProviders(): OAuthProvider[] {
    return this.getProviders().filter(p => p.enabled);
  }

  /**
   * Get a provider by ID from the cached resource.
   */
  getProviderById(providerId: string): OAuthProvider | undefined {
    return this.getProviders().find(p => p.providerId === providerId);
  }

  /**
   * Fetch all OAuth Providers from the API.
   */
  async fetchProviders(): Promise<OAuthProviderListResponse> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl}/`)
    );
    // Convert snake_case response to camelCase
    return {
      providers: response.providers.map((p: any) => toCamelCase(p) as OAuthProvider),
      total: response.total,
    };
  }

  /**
   * Fetch a single provider by ID from the API.
   */
  async fetchProvider(providerId: string): Promise<OAuthProvider> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl}/${providerId}`)
    );
    // Convert snake_case response to camelCase
    return toCamelCase(response) as OAuthProvider;
  }

  /**
   * Create a new OAuth Provider.
   */
  async createProvider(providerData: OAuthProviderCreateRequest): Promise<OAuthProvider> {
    // Convert camelCase request to snake_case
    const snakeCaseData = toSnakeCase(providerData as unknown as Record<string, any>);
    const response = await firstValueFrom(
      this.http.post<any>(`${this.baseUrl}/`, snakeCaseData)
    );
    this.providersResource.reload();
    // Convert snake_case response to camelCase
    return toCamelCase(response) as OAuthProvider;
  }

  /**
   * Update an existing OAuth Provider.
   */
  async updateProvider(providerId: string, updates: OAuthProviderUpdateRequest): Promise<OAuthProvider> {
    // Convert camelCase request to snake_case
    const snakeCaseData = toSnakeCase(updates as unknown as Record<string, any>);
    const response = await firstValueFrom(
      this.http.patch<any>(`${this.baseUrl}/${providerId}`, snakeCaseData)
    );
    this.providersResource.reload();
    // Convert snake_case response to camelCase
    return toCamelCase(response) as OAuthProvider;
  }

  /**
   * Delete an OAuth Provider.
   */
  async deleteProvider(providerId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl}/${providerId}`)
    );
    this.providersResource.reload();
  }

  /**
   * Reload the providers resource.
   */
  reload(): void {
    this.providersResource.reload();
  }
}
