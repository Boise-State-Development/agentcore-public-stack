import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import { AppRole, AppRoleListResponse } from '../models/app-role.model';

/**
 * Service to manage AppRoles.
 *
 * Provides access to the role list for use in forms and displays.
 */
@Injectable({
  providedIn: 'root'
})
export class AppRolesService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

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
   * Get a role by ID.
   */
  getRoleById(roleId: string): AppRole | undefined {
    return this.getRoles().find(r => r.roleId === roleId);
  }

  /**
   * Fetch all AppRoles from the API.
   */
  async fetchRoles(): Promise<AppRoleListResponse> {
    const response = await firstValueFrom(
      this.http.get<AppRoleListResponse>(
        `${environment.appApiUrl}/admin/roles/`
      )
    );
    return response;
  }

  /**
   * Reload the roles resource.
   */
  reload(): void {
    this.rolesResource.reload();
  }
}
