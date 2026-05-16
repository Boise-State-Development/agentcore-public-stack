import { Injectable, inject, computed, resource, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  UserMenuLink,
  UserMenuLinkFormData,
  UserMenuLinksListResponse,
} from '../models/user-menu-link.model';

/**
 * Service for admin-managed user-menu links.
 *
 * Two API surfaces:
 *  - Admin: `/admin/user-menu-links` (CRUD, includes disabled links)
 *  - Public: `/user-menu-links` (enabled-only, used by `enabledLinksResource`)
 *
 * The public resource is what the user-dropdown component consumes. The
 * dropdown takes a `User` as a required input and is only rendered by the
 * topnav once the session bootstrap has resolved, so the resource's loader
 * fires post-auth on first read — no explicit reload needed.
 */
@Injectable({ providedIn: 'root' })
export class UserMenuLinksService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly adminBaseUrl = computed(
    () => `${this.config.appApiUrl()}/admin/user-menu-links`,
  );
  private readonly publicBaseUrl = computed(
    () => `${this.config.appApiUrl()}/user-menu-links`,
  );

  // The admin resource is gated: this service is `providedIn: 'root'` and is
  // injected by the always-rendered user-dropdown, so an eager admin loader
  // would fire `GET /admin/user-menu-links/` on every app load for every user
  // (401/403 for non-admins). It only loads once the admin manage page calls
  // `ensureAdminLinksLoaded()`.
  private readonly adminLinksRequested = signal(false);

  readonly adminLinksResource = resource({
    params: () => (this.adminLinksRequested() ? {} : undefined),
    loader: async () => this.fetchAdminLinks(),
  });

  /** Activates the admin links resource. Called by the admin manage page. */
  ensureAdminLinksLoaded(): void {
    this.adminLinksRequested.set(true);
  }

  readonly enabledLinksResource = resource({
    loader: async () => this.fetchEnabledLinks(),
  });

  async fetchAdminLinks(): Promise<UserMenuLinksListResponse> {
    return await firstValueFrom(
      this.http.get<UserMenuLinksListResponse>(`${this.adminBaseUrl()}/`),
    );
  }

  async fetchEnabledLinks(): Promise<UserMenuLinksListResponse> {
    return await firstValueFrom(
      this.http.get<UserMenuLinksListResponse>(`${this.publicBaseUrl()}/`),
    );
  }

  async getLink(linkId: string): Promise<UserMenuLink> {
    return await firstValueFrom(
      this.http.get<UserMenuLink>(`${this.adminBaseUrl()}/${linkId}`),
    );
  }

  async createLink(data: UserMenuLinkFormData): Promise<UserMenuLink> {
    const created = await firstValueFrom(
      this.http.post<UserMenuLink>(`${this.adminBaseUrl()}/`, data),
    );
    this.adminLinksResource.reload();
    this.enabledLinksResource.reload();
    return created;
  }

  async updateLink(
    linkId: string,
    updates: Partial<UserMenuLinkFormData>,
  ): Promise<UserMenuLink> {
    const updated = await firstValueFrom(
      this.http.patch<UserMenuLink>(`${this.adminBaseUrl()}/${linkId}`, updates),
    );
    this.adminLinksResource.reload();
    this.enabledLinksResource.reload();
    return updated;
  }

  async deleteLink(linkId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.adminBaseUrl()}/${linkId}`),
    );
    this.adminLinksResource.reload();
    this.enabledLinksResource.reload();
  }
}
