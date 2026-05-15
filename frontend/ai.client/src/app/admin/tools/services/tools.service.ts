import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { Tool, ToolListResponse, UserToolPermissions } from '../models/tool.model';

/**
 * Service for tool catalog and permissions.
 */
@Injectable({
  providedIn: 'root'
})
export class ToolsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => this.config.appApiUrl());

  /**
   * Reactive resource for fetching the tool catalog.
   */
  readonly catalogResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchCatalog();
    }
  });

  /**
   * Get all tools from the catalog.
   */
  getTools(): Tool[] {
    return this.catalogResource.value()?.tools ?? [];
  }

  /**
   * Get tools grouped by category.
   */
  getToolsByCategory(): Map<string, Tool[]> {
    const tools = this.getTools();
    const grouped = new Map<string, Tool[]>();

    for (const tool of tools) {
      const category = tool.category;
      if (!grouped.has(category)) {
        grouped.set(category, []);
      }
      grouped.get(category)!.push(tool);
    }

    return grouped;
  }

  /**
   * Get a tool by ID.
   */
  getToolById(toolId: string): Tool | undefined {
    return this.getTools().find(t => t.toolId === toolId);
  }

  /**
   * Fetch tool catalog from API.
   */
  async fetchCatalog(): Promise<ToolListResponse> {
    const response = await firstValueFrom(
      this.http.get<ToolListResponse>(
        `${this.baseUrl()}/tools/catalog`
      )
    );
    return response;
  }

  /**
   * Fetch admin tool catalog (full list).
   * @deprecated Use AdminToolService.fetchTools() instead
   */
  async fetchAdminCatalog(): Promise<ToolListResponse> {
    const response = await firstValueFrom(
      this.http.get<ToolListResponse>(
        `${this.baseUrl()}/admin/tools/`
      )
    );
    return response;
  }

  /**
   * Fetch current user's tool permissions.
   */
  async fetchMyPermissions(): Promise<UserToolPermissions> {
    const response = await firstValueFrom(
      this.http.get<UserToolPermissions>(
        `${this.baseUrl()}/tools/my-permissions`
      )
    );
    return response;
  }

  /**
   * Reload the catalog resource.
   */
  reload(): void {
    this.catalogResource.reload();
  }
}
