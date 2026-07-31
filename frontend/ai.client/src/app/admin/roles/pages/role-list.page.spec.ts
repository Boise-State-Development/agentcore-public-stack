/**
 * The roles list surfaces delegated admin power.
 *
 * The list card previously showed JWT mappings and tool grants but nothing
 * about admin scopes, so a system admin scanning the page could not tell which
 * roles carry admin power without opening each edit form. That cuts against the
 * spec's own invariant that granting a scope is a `system_admin`-only act worth
 * watching (`docs/specs/granular-admin-permissions.md` §3 I2).
 *
 * The subtle case these tests pin down: `system_admin` holds every scope
 * *implicitly* and therefore carries an empty `grantedAdminScopes`. Rendering
 * that as "no admin access" would be exactly backwards.
 */
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, it, expect, beforeEach } from 'vitest';

import { RoleListPage } from './role-list.page';
import { AppRolesService } from '../services/app-roles.service';
import { ToolsService } from '../../tools/services/tools.service';
import { AppRole } from '../models/app-role.model';
import { AdminScopeListResponse } from '../../admin-scope.model';

function role(overrides: Partial<AppRole> = {}): AppRole {
  return {
    roleId: 'analyst',
    displayName: 'Analyst',
    description: '',
    jwtRoleMappings: [],
    inheritsFrom: [],
    grantedTools: [],
    grantedModels: [],
    grantedSkills: [],
    grantedAdminScopes: [],
    priority: 0,
    enabled: true,
    isSystemRole: false,
    ...overrides,
  } as AppRole;
}

const REGISTRY: AdminScopeListResponse = {
  total: 2,
  scopes: [
    {
      id: 'admin.costs',
      label: 'Cost Analytics',
      group: 'Usage & Spend',
      description: '',
      delegable: true,
    },
    {
      id: 'admin.users',
      label: 'Users',
      group: 'Identity & Access',
      description: '',
      delegable: true,
    },
  ],
};

class StubAppRolesService {
  readonly rolesResource = { isLoading: () => false, value: () => undefined };
  getRoles(): AppRole[] {
    return [];
  }
  async fetchAdminScopes(): Promise<AdminScopeListResponse> {
    return REGISTRY;
  }
}

class StubToolsService {
  getToolById() {
    return undefined;
  }
}

describe('RoleListPage — delegated admin visibility', () => {
  let page: RoleListPage;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AppRolesService, useClass: StubAppRolesService },
        { provide: ToolsService, useClass: StubToolsService },
      ],
    });
    page = TestBed.createComponent(RoleListPage).componentInstance;
    // The registry load is fired from the constructor; let it settle so the
    // label lookups below resolve to real labels rather than raw ids.
    await Promise.resolve();
    await Promise.resolve();
  });

  it('shows no badge for a role with no admin scopes', () => {
    expect(page.adminAccessBadge(role())).toBeNull();
  });

  it('badges system_admin as full admin despite an empty scope list', () => {
    const badge = page.adminAccessBadge(
      role({ roleId: 'system_admin', isSystemRole: true, grantedAdminScopes: [] })
    );
    expect(badge?.label).toBe('Full Admin');
  });

  it('badges a delegated role and counts its scopes', () => {
    const badge = page.adminAccessBadge(
      role({ grantedAdminScopes: ['admin.costs', 'admin.users'] })
    );
    expect(badge?.label).toBe('Admin Access · 2');
  });

  it('drops the count for a single scope', () => {
    const badge = page.adminAccessBadge(
      role({ grantedAdminScopes: ['admin.costs'] })
    );
    expect(badge?.label).toBe('Admin Access');
  });

  it('names the granted areas in the badge tooltip', () => {
    const badge = page.adminAccessBadge(
      role({ grantedAdminScopes: ['admin.costs', 'admin.users'] })
    );
    expect(badge?.title).toBe('Delegated admin areas: Cost Analytics, Users');
  });

  it('resolves scope ids to registry labels', () => {
    expect(page.getAdminScopeLabel('admin.costs')).toBe('Cost Analytics');
  });

  it('falls back to the raw id for a scope missing from the registry', () => {
    expect(page.getAdminScopeLabel('admin.not_in_registry')).toBe(
      'admin.not_in_registry'
    );
  });
});
