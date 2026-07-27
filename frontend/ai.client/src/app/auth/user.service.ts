import { inject, Injectable, signal, computed, effect } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SessionService } from './session.service';
import { ConfigService } from '../services/config.service';
import { User, UserPermissions } from './user.model';

/**
 * UserService — derived view over the BFF session.
 *
 * Phase 7 collapsed user identity onto `SessionService.user()` (sourced
 * from `GET /auth/session`); UserService stays around as the
 * legacy-shaped facade the rest of the SPA was built against. It maps
 * the BFF user payload to the historical `User` model (with
 * firstName/lastName/fullName split out of the single `name` field) so
 * components that read `currentUser()` keep working without churn.
 */
@Injectable({
  providedIn: 'root'
})
export class UserService {
  private readonly sessionService = inject(SessionService);
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  readonly currentUser = computed<User | null>(() => {
    const sessionUser = this.sessionService.user();
    if (!sessionUser) {
      return null;
    }
    const fullName = sessionUser.name?.trim() || sessionUser.email || '';
    const [firstName = '', ...rest] = fullName.split(' ');
    const lastName = rest.join(' ');
    return {
      email: sessionUser.email,
      user_id: sessionUser.user_id || sessionUser.email,
      firstName,
      lastName,
      fullName,
      roles: sessionUser.roles || [],
      picture: sessionUser.picture ?? undefined,
    };
  });

  readonly appRoles = signal<string[]>([]);

  /**
   * Delegated admin feature areas granted to this user.
   *
   * Empty for a full admin: `system_admin` satisfies every scope implicitly
   * rather than enumerating them, so never test membership here to decide
   * whether someone is an admin — use `hasAdminScope` or `isAdmin`.
   */
  readonly adminScopes = signal<string[]>([]);

  readonly isAdmin = computed(() => this.appRoles().includes('system_admin'));

  /**
   * May this user open the admin console at all?
   *
   * Distinct from `isAdmin`, which keeps its exact previous meaning and still
   * gates genuinely superuser-only surfaces (roles, auth providers).
   */
  readonly canAccessAdmin = computed(
    () => this.isAdmin() || this.adminScopes().length > 0
  );

  private _permissionsPromise: Promise<void> | null = null;

  constructor() {
    effect(() => {
      const authed = this.sessionService.isAuthenticated();
      if (authed) {
        if (!this._permissionsPromise) {
          this._permissionsPromise = this.fetchPermissions();
        }
      } else {
        this._permissionsPromise = null;
        this.appRoles.set([]);
        this.adminScopes.set([]);
      }
    });
  }

  getUser(): User | null {
    return this.currentUser();
  }

  hasRole(role: string): boolean {
    return this.currentUser()?.roles.includes(role) ?? false;
  }

  hasAnyRole(roles: string[]): boolean {
    const user = this.currentUser();
    if (!user) return false;
    return roles.some(role => user.roles.includes(role));
  }

  async fetchPermissions(): Promise<void> {
    try {
      const url = `${this.config.appApiUrl()}/users/me/permissions`;
      const permissions = await firstValueFrom(
        this.http.get<UserPermissions>(url, { withCredentials: true })
      );
      this.appRoles.set(permissions.appRoles);
      // `?? []` rather than a bare read: an app-api still on a pre-PR-3 build
      // omits the field entirely, and `undefined` here would make
      // `adminScopes().length` throw inside `canAccessAdmin` and break the
      // admin entry point for full admins too.
      this.adminScopes.set(permissions.adminScopes ?? []);
    } catch (error) {
      console.error('Failed to fetch user permissions:', error);
      this.appRoles.set([]);
      this.adminScopes.set([]);
    }
  }

  async ensurePermissionsLoaded(): Promise<void> {
    if (this._permissionsPromise) {
      await this._permissionsPromise;
      return;
    }
    if (this.sessionService.isAuthenticated()) {
      this._permissionsPromise = this.fetchPermissions();
      await this._permissionsPromise;
    }
  }

  hasAppRole(role: string): boolean {
    return this.appRoles().includes(role);
  }

  /**
   * Does this user hold a given delegated admin scope?
   *
   * `system_admin` short-circuits to true — it satisfies every scope
   * implicitly, exactly as `require_admin_scope` does on the server.
   */
  hasAdminScope(scope: string): boolean {
    return this.isAdmin() || this.adminScopes().includes(scope);
  }

  refreshUser(): void {
    // No-op: currentUser is computed off SessionService.user(), which
    // updates automatically as the session is bootstrapped or invalidated.
  }
}
