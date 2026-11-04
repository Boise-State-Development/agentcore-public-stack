import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AppRoleOption, AppRoleOptionListResponse } from '../models/app-role.model';

/**
 * The role list for "which roles can use this?" pickers outside the roles pages.
 *
 * Reads `GET /admin/roles/picker-options`, which any delegated admin may call,
 * rather than {@link AppRolesService}'s full list, which is `system_admin`-only
 * and would 403 for a scoped Models, Tools or Skills admin. Read-only: a picker's
 * choice is saved by its own domain's write-through, never here.
 *
 * Inject this, not `AppRolesService`, on any page a delegated admin can reach —
 * constructing `AppRolesService` starts its full-list fetch.
 */
@Injectable({
  providedIn: 'root'
})
export class RoleOptionsService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly url = computed(() => `${this.config.appApiUrl()}/admin/roles/picker-options`);

  readonly rolesResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchRoles();
    }
  });

  /** Every role, highest priority first. Empty while loading. */
  getRoles(): AppRoleOption[] {
    return this.rolesResource.value()?.roles ?? [];
  }

  getEnabledRoles(): AppRoleOption[] {
    return this.getRoles().filter(r => r.enabled);
  }

  getRoleById(roleId: string): AppRoleOption | undefined {
    return this.getRoles().find(r => r.roleId === roleId);
  }

  async fetchRoles(): Promise<AppRoleOptionListResponse> {
    return firstValueFrom(this.http.get<AppRoleOptionListResponse>(this.url()));
  }

  reload(): void {
    this.rolesResource.reload();
  }
}
