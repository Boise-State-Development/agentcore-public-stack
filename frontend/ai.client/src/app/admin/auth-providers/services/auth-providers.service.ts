import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AuthProvider,
  AuthProviderListResponse,
  AuthProviderCreateRequest,
  AuthProviderUpdateRequest,
  OIDCDiscoveryResponse,
} from '../models/auth-provider.model';

@Injectable({
  providedIn: 'root'
})
export class AuthProvidersService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/auth-providers`);

  readonly providersResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchProviders();
    }
  });

  getProviders(): AuthProvider[] {
    return this.providersResource.value()?.providers ?? [];
  }

  getProviderById(providerId: string): AuthProvider | undefined {
    return this.getProviders().find(p => p.provider_id === providerId);
  }

  async fetchProviders(): Promise<AuthProviderListResponse> {
    return firstValueFrom(
      this.http.get<AuthProviderListResponse>(`${this.baseUrl()}/`)
    );
  }

  async fetchProvider(providerId: string): Promise<AuthProvider> {
    return firstValueFrom(
      this.http.get<AuthProvider>(`${this.baseUrl()}/${providerId}`)
    );
  }

  async createProvider(data: AuthProviderCreateRequest): Promise<AuthProvider> {
    const response = await firstValueFrom(
      this.http.post<AuthProvider>(`${this.baseUrl()}/`, data)
    );
    this.providersResource.reload();
    return response;
  }

  async updateProvider(providerId: string, updates: AuthProviderUpdateRequest): Promise<AuthProvider> {
    const response = await firstValueFrom(
      this.http.patch<AuthProvider>(`${this.baseUrl()}/${providerId}`, updates)
    );
    this.providersResource.reload();
    return response;
  }

  async deleteProvider(providerId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/${providerId}`)
    );
    this.providersResource.reload();
  }

  async discoverEndpoints(issuerUrl: string): Promise<OIDCDiscoveryResponse> {
    return firstValueFrom(
      this.http.post<OIDCDiscoveryResponse>(`${this.baseUrl()}/discover`, { issuer_url: issuerUrl })
    );
  }

  async testProvider(providerId: string): Promise<{ status: string; details: Record<string, unknown> }> {
    return firstValueFrom(
      this.http.post<{ status: string; details: Record<string, unknown> }>(`${this.baseUrl()}/${providerId}/test`, {})
    );
  }

  async getCurrentImageTag(): Promise<{ image_tag: string }> {
    return firstValueFrom(
      this.http.get<{ image_tag: string }>(`${this.baseUrl()}/runtime-image-tag`)
    );
  }

  async getCognitoRedirectUri(): Promise<string> {
    const response = await firstValueFrom(
      this.http.get<{ redirect_uri: string }>(`${this.baseUrl()}/cognito-redirect-uri`)
    );
    return response.redirect_uri;
  }

  async triggerRuntimeUpdate(providerId: string): Promise<{ message: string }> {
    return firstValueFrom(
      this.http.post<{ message: string }>(`${this.baseUrl()}/${providerId}/update-runtime`, {})
    );
  }

  reload(): void {
    this.providersResource.reload();
  }
}
