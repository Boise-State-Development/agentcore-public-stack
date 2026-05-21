import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  Connector,
  ConnectorListResponse,
  ConnectorCreateRequest,
  ConnectorUpdateRequest,
  FileSourceAdapter,
  FileSourceAdapterListResponse,
} from '../models/connector.model';

function toSnakeCase(obj: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};
  for (const [key, value] of Object.entries(obj)) {
    if (value === undefined) continue;
    const snakeKey = key.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`);
    result[snakeKey] = value;
  }
  return result;
}

function toCamelCase(obj: Record<string, any>): Record<string, any> {
  const result: Record<string, any> = {};
  for (const [key, value] of Object.entries(obj)) {
    const camelKey = key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
    result[camelKey] = value;
  }
  return result;
}

/**
 * Admin service for managing connectors.
 *
 * The backend admin endpoint is still `/admin/oauth-providers` — that is the
 * stable wire contract. We use the connectors vernacular throughout the
 * frontend and translate at this layer.
 */
@Injectable({
  providedIn: 'root'
})
export class ConnectorsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/oauth-providers`);

  private readonly fileSourceAdaptersUrl = computed(
    () => `${this.config.appApiUrl()}/admin/file-source-adapters`
  );

  readonly connectorsResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchConnectors();
    }
  });

  /**
   * The file-source adapter registry. Read-only and rarely changes — adapters
   * ship in releases — so a single eager load when the admin service is first
   * injected is sufficient.
   */
  readonly fileSourceAdaptersResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchFileSourceAdapters();
    }
  });

  getConnectors(): Connector[] {
    return this.connectorsResource.value()?.providers ?? [];
  }

  getEnabledConnectors(): Connector[] {
    return this.getConnectors().filter(c => c.enabled);
  }

  getConnectorById(providerId: string): Connector | undefined {
    return this.getConnectors().find(c => c.providerId === providerId);
  }

  async fetchConnectors(): Promise<ConnectorListResponse> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl()}/`)
    );
    return {
      providers: response.providers.map((p: any) => toCamelCase(p) as Connector),
      total: response.total,
    };
  }

  getFileSourceAdapters(): FileSourceAdapter[] {
    return this.fileSourceAdaptersResource.value()?.adapters ?? [];
  }

  /**
   * Fetch the file-source adapter registry. This endpoint serializes
   * camelCase already, so no key translation is applied.
   */
  async fetchFileSourceAdapters(): Promise<FileSourceAdapterListResponse> {
    return firstValueFrom(
      this.http.get<FileSourceAdapterListResponse>(`${this.fileSourceAdaptersUrl()}/`)
    );
  }

  async fetchConnector(providerId: string): Promise<Connector> {
    const response = await firstValueFrom(
      this.http.get<any>(`${this.baseUrl()}/${providerId}`)
    );
    return toCamelCase(response) as Connector;
  }

  async createConnector(data: ConnectorCreateRequest): Promise<Connector> {
    const snakeCaseData = toSnakeCase(data as unknown as Record<string, any>);
    const response = await firstValueFrom(
      this.http.post<any>(`${this.baseUrl()}/`, snakeCaseData)
    );
    this.connectorsResource.reload();
    return toCamelCase(response) as Connector;
  }

  async updateConnector(providerId: string, updates: ConnectorUpdateRequest): Promise<Connector> {
    const snakeCaseData = toSnakeCase(updates as unknown as Record<string, any>);
    const response = await firstValueFrom(
      this.http.patch<any>(`${this.baseUrl()}/${providerId}`, snakeCaseData)
    );
    this.connectorsResource.reload();
    return toCamelCase(response) as Connector;
  }

  async deleteConnector(providerId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/${providerId}`)
    );
    this.connectorsResource.reload();
  }

  reload(): void {
    this.connectorsResource.reload();
  }
}
