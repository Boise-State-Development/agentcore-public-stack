import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import {
  AppRole,
  AppRoleListResponse,
  AppRoleCreateRequest,
  AppRoleUpdateRequest,
} from '../models/app-role.model';

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
  private authService = inject(AuthService);

  private readonly baseUrl = `${environment.appApiUrl}/admin/roles`;

  /**
   * Reactive resource for fetching AppRoles.
   */
  readonly rolesResource = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
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
      this.http.get<AppRoleListResponse>(`${this.baseUrl}/`)
    );
    return response;
  }

  /**
   * Fetch a single role by ID from the API.
   */
  async fetchRole(roleId: string): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.get<AppRole>(`${this.baseUrl}/${roleId}`)
    );
    return response;
  }

  /**
   * Create a new AppRole.
   */
  async createRole(roleData: AppRoleCreateRequest): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.post<AppRole>(`${this.baseUrl}/`, roleData)
    );
    this.rolesResource.reload();
    return response;
  }

  /**
   * Update an existing AppRole.
   */
  async updateRole(roleId: string, updates: AppRoleUpdateRequest): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.patch<AppRole>(`${this.baseUrl}/${roleId}`, updates)
    );
    this.rolesResource.reload();
    return response;
  }

  /**
   * Delete an AppRole.
   */
  async deleteRole(roleId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl}/${roleId}`)
    );
    this.rolesResource.reload();
  }

  /**
   * Force recompute effective permissions for a role.
   */
  async syncPermissions(roleId: string): Promise<AppRole> {
    const response = await firstValueFrom(
      this.http.post<AppRole>(`${this.baseUrl}/${roleId}/sync`, {})
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
