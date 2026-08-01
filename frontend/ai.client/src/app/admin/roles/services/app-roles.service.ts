import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AppRole,
  AppRoleListResponse,
  AppRoleCreateRequest,
  AppRoleUpdateRequest,
} from '../models/app-role.model';
import { AdminScopeListResponse } from '../../admin-scope.model';

/**
 * Service to manage AppRoles.
 *
 * Provides access to the role list for use in forms and displays,
 * as well as CRUD operations for role management.
 */
@Injectable({
  providedIn: 'root'
})
export class AppRolesService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/roles`);

  /**
   * Reactive resource for fetching AppRoles.
   */
  readonly rolesResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchRoles();
    }
  });

  /**
   * Get all AppRoles (from resource).
   */
  getRoles(): AppRole[] {
    return this.rolesResource.value()?.roles ?? [];
  }

  /**
   * Get only enabled AppRoles.
   */
  getEnabledRoles(): AppRole[] {
    return this.getRoles().filter(r => r.enabled);
  }

  /**
   * Get a role by ID from the cached resource.
   */
  getRoleById(roleId: string): AppRole | undefined {
    return this.getRoles().find(r => r.roleId === roleId);
  }

  /**
   * Fetch all AppRoles from the API.
   */
  async fetchRoles(): Promise<AppRoleListResponse> {
    const response = await firstValueFrom(
      this.http.get<AppRoleListResponse>(`${this.baseUrl()}/`)
    );
    return response;
  }

  /**
   * Fetch the delegated admin scope registry.
   *
   * Served from code rather than derived from a catalog, so the role form's
   * picker always offers exactly the scopes the server will accept. Includes
   * non-delegable entries with `delegable: false` — the picker shows them as
   * unavailable rather than omitting them, so an admin can see that Roles and
   * Auth Providers exist and are deliberately withheld.
   */
  async fetchAdminScopes(): Promise<AdminScopeListResponse> {
    return firstValueFrom(
      this.http.get<AdminScopeListResponse>(`${this.baseUrl()}/admin-scopes`)
    );
  }

  /**
   * Fetch a single role by ID from the API.
   */
  async fetchRole(roleId: string): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.get<AppRole>(`${this.baseUrl()}/${roleId}`)
    );
    return response;
  }

  /**
   * Create a new AppRole.
   */
  async createRole(roleData: AppRoleCreateRequest): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.post<AppRole>(`${this.baseUrl()}/`, roleData)
    );
    this.rolesResource.reload();
    return response;
  }

  /**
   * Update an existing AppRole.
   */
  async updateRole(roleId: string, updates: AppRoleUpdateRequest): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.patch<AppRole>(`${this.baseUrl()}/${roleId}`, updates)
    );
    this.rolesResource.reload();
    return response;
  }

  /**
   * Delete an AppRole.
   */
  async deleteRole(roleId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/${roleId}`)
    );
    this.rolesResource.reload();
  }

  /**
   * Force recompute effective permissions for a role.
   */
  async syncPermissions(roleId: string): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.post<AppRole>(`${this.baseUrl()}/${roleId}/sync`, {})
    );
    this.rolesResource.reload();
    return response;
  }

  /**
   * Reload the roles resource.
   */
  reload(): void {
    this.rolesResource.reload();
  }
}
