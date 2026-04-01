import { Injectable, inject, resource, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import {
  AdminTool,
  AdminToolListResponse,
  ToolCreateRequest,
  ToolUpdateRequest,
  ToolRolesResponse,
  ToolRoleAssignment,
  SyncResult,
} from '../models/admin-tool.model';

/**
 * Service for admin tool management.
 *
 * Provides CRUD operations for the tool catalog and role assignments.
 */
@Injectable({
  providedIn: 'root'
})
export class AdminToolService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/tools`);

  // Signals for local state
  private _loading = signal(false);
  private _error = signal<string | null>(null);

  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /**
   * Reactive resource for fetching admin tools.
   */
  readonly toolsResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchTools();
    }
  });

  /**
   * Get all tools from the resource.
   */
  getTools(): AdminTool[] {
    return this.toolsResource.value()?.tools ?? [];
  }

  /**
   * Get a tool by ID from the cached resource.
   */
  getToolById(toolId: string): AdminTool | undefined {
    return this.getTools().find(t => t.toolId === toolId);
  }

  /**
   * Fetch all tools from the API.
   */
  async fetchTools(status?: string): Promise<AdminToolListResponse> {
    let url = `${this.baseUrl()}/`;
    if (status) {
      url += `?status=${status}`;
    }
    const response = await firstValueFrom(
      this.http.get<AdminToolListResponse>(url)
    );
    return response;
  }

  /**
   * Fetch a single tool by ID.
   */
  async fetchTool(toolId: string): Promise<AdminTool> {
    const response = await firstValueFrom(
      this.http.get<AdminTool>(`${this.baseUrl()}/${toolId}`)
    );
    return response;
  }

  /**
   * Create a new tool.
   */
  async createTool(toolData: ToolCreateRequest): Promise<AdminTool> {
    this._loading.set(true);
    this._error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.post<AdminTool>(`${this.baseUrl()}/`, toolData)
      );
      this.toolsResource.reload();
      return response;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create tool';
      this._error.set(message);
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Update an existing tool.
   */
  async updateTool(toolId: string, updates: ToolUpdateRequest): Promise<AdminTool> {
    this._loading.set(true);
    this._error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.put<AdminTool>(`${this.baseUrl()}/${toolId}`, updates)
      );
      this.toolsResource.reload();
      return response;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to update tool';
      this._error.set(message);
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Delete a tool (soft delete by default).
   */
  async deleteTool(toolId: string, hard: boolean = false): Promise<void> {
    this._loading.set(true);
    this._error.set(null);

    try {
      await firstValueFrom(
        this.http.delete<void>(`${this.baseUrl()}/${toolId}?hard=${hard}`)
      );
      this.toolsResource.reload();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to delete tool';
      this._error.set(message);
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Get roles that grant access to a tool.
   */
  async getToolRoles(toolId: string): Promise<ToolRoleAssignment[]> {
    const response = await firstValueFrom(
      this.http.get<ToolRolesResponse>(`${this.baseUrl()}/${toolId}/roles`)
    );
    return response.roles;
  }

  /**
   * Set which roles grant access to a tool.
   */
  async setToolRoles(toolId: string, roleIds: string[]): Promise<void> {
    this._loading.set(true);
    this._error.set(null);

    try {
      await firstValueFrom(
        this.http.put(`${this.baseUrl()}/${toolId}/roles`, { appRoleIds: roleIds })
      );
      this.toolsResource.reload();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to set tool roles';
      this._error.set(message);
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Add roles to tool access.
   */
  async addRolesToTool(toolId: string, roleIds: string[]): Promise<void> {
    await firstValueFrom(
      this.http.post(`${this.baseUrl()}/${toolId}/roles/add`, { appRoleIds: roleIds })
    );
    this.toolsResource.reload();
  }

  /**
   * Remove roles from tool access.
   */
  async removeRolesFromTool(toolId: string, roleIds: string[]): Promise<void> {
    await firstValueFrom(
      this.http.post(`${this.baseUrl()}/${toolId}/roles/remove`, { appRoleIds: roleIds })
    );
    this.toolsResource.reload();
  }

  /**
   * Sync catalog from code registry.
   */
  async syncFromRegistry(dryRun: boolean = true): Promise<SyncResult> {
    this._loading.set(true);
    this._error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.post<SyncResult>(`${this.baseUrl()}/sync?dry_run=${dryRun}`, {})
      );
      if (!dryRun) {
        this.toolsResource.reload();
      }
      return response;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to sync catalog';
      this._error.set(message);
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Reload the tools resource.
   */
  reload(): void {
    this.toolsResource.reload();
  }
}
