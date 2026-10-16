/**
 * The role form's four grant pickers, drawn by the shared `<app-tool-selector>`.
 *
 * The role record's own `granted*` lists are what access checks read (CLAUDE.md,
 * "RBAC: the AppRole record is the source of truth"), so what these pin down is
 * the round trip: a loaded role checks the right rows, a toggle edits exactly
 * that control's array, and Save sends the arrays in the same request shape as
 * before. Ids the picker has no row for must survive the edit, not be dropped.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RoleFormPage } from './role-form.page';
import { AppRolesService } from '../services/app-roles.service';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { ManagedModelsService } from '../../manage-models/services/managed-models.service';
import { AppRole } from '../models/app-role.model';
import { AdminScopeListResponse } from '../../admin-scope.model';

function role(overrides: Partial<AppRole> = {}): AppRole {
  return {
    roleId: 'analyst',
    displayName: 'Analyst',
    description: 'Reads reports',
    jwtRoleMappings: ['Analysts'],
    inheritsFrom: [],
    grantedTools: [],
    grantedModels: [],
    grantedSkills: [],
    grantedAdminScopes: [],
    priority: 10,
    enabled: true,
    isSystemRole: false,
    ...overrides,
  } as AppRole;
}

const ROLES: AppRole[] = [
  role({ roleId: 'analyst', displayName: 'Analyst' }),
  role({ roleId: 'basic_user', displayName: 'Basic User', description: 'Everyone' }),
  role({ roleId: 'power_user', displayName: 'Power User', description: 'Extra tools' }),
];

const TOOLS = [
  { toolId: 'web_search', displayName: 'Web Search', description: 'Search the web', category: 'search', status: 'active' },
  { toolId: 'calculator', displayName: 'Calculator', description: 'Arithmetic', category: 'utility', status: 'active' },
  { toolId: 'old_search', displayName: 'Old Search', description: 'Legacy', category: 'search', status: 'deprecated' },
];

const MODELS = [
  { id: 'm1', modelId: 'us.anthropic.claude-haiku-4-5', modelName: 'Claude Haiku 4.5', providerName: 'Anthropic', enabled: true },
  { id: 'm2', modelId: 'amazon.nova-micro-v1:0', modelName: 'Nova Micro', providerName: 'Amazon', enabled: false },
];

const SCOPES: AdminScopeListResponse = {
  total: 3,
  scopes: [
    { id: 'admin.costs', label: 'Cost Analytics', group: 'Usage & Spend', description: 'Spend', delegable: true },
    { id: 'admin.users', label: 'Users', group: 'Identity & Access', description: 'People', delegable: true },
    { id: 'admin.roles', label: 'Roles', group: 'Identity & Access', description: 'Grant power', delegable: false },
  ],
};

describe('RoleFormPage — grant pickers', () => {
  let fixture: ComponentFixture<RoleFormPage>;
  let el: HTMLElement;
  let service: {
    getEnabledRoles: ReturnType<typeof vi.fn>;
    fetchAdminScopes: ReturnType<typeof vi.fn>;
    fetchRole: ReturnType<typeof vi.fn>;
    updateRole: ReturnType<typeof vi.fn>;
    createRole: ReturnType<typeof vi.fn>;
  };

  async function mount(id: string, loaded: AppRole = role()): Promise<void> {
    service = {
      getEnabledRoles: vi.fn(() => ROLES),
      fetchAdminScopes: vi.fn().mockResolvedValue(SCOPES),
      fetchRole: vi.fn().mockResolvedValue(loaded),
      updateRole: vi.fn().mockResolvedValue(loaded),
      createRole: vi.fn().mockResolvedValue(loaded),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'admin/roles', children: [] }]),
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id }) } } },
        { provide: AppRolesService, useValue: service },
        {
          provide: AdminToolService,
          useValue: { toolsResource: { isLoading: () => false }, getTools: () => TOOLS },
        },
        {
          provide: ManagedModelsService,
          useValue: { modelsResource: { isLoading: () => false }, getManagedModels: () => MODELS },
        },
      ],
    });
    fixture = TestBed.createComponent(RoleFormPage);
    el = fixture.nativeElement;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  afterEach(() => TestBed.resetTestingModule());

  function picker(headingId: string): HTMLElement {
    const list = el.querySelector<HTMLElement>(`[role="group"][aria-labelledby="${headingId}"]`);
    expect(list, `picker labelled by #${headingId}`).toBeTruthy();
    return list!;
  }

  function rowNames(headingId: string): (string | undefined)[] {
    return [...picker(headingId).querySelectorAll('span[id$="-name"]')].map((s) => s.textContent?.trim());
  }

  function checkbox(headingId: string, name: string): HTMLInputElement {
    const list = picker(headingId);
    const label = [...list.querySelectorAll('span[id$="-name"]')].find((s) => s.textContent?.trim() === name);
    expect(label, `row "${name}"`).toBeTruthy();
    return list.querySelector<HTMLInputElement>(`input[aria-labelledby="${label!.id}"]`)!;
  }

  function click(input: HTMLInputElement): void {
    input.click();
    fixture.detectChanges();
  }

  it('draws all four grants with the shared selector, named by their section headings', async () => {
    await mount('new');
    expect(el.querySelectorAll('app-tool-selector')).toHaveLength(4);
    for (const id of ['role-inherits-heading', 'role-tools-heading', 'role-admin-heading', 'role-models-heading']) {
      expect(el.querySelector(`#${id}`)).toBeTruthy();
      picker(id);
    }
  });

  it('checks the loaded role’s grants and leaves the rest unchecked', async () => {
    await mount(
      'analyst',
      role({
        inheritsFrom: ['basic_user'],
        grantedTools: ['web_search'],
        grantedModels: ['amazon.nova-micro-v1:0'],
        grantedAdminScopes: ['admin.costs'],
      }),
    );
    expect(checkbox('role-inherits-heading', 'Basic User').checked).toBe(true);
    expect(checkbox('role-inherits-heading', 'Power User').checked).toBe(false);
    expect(checkbox('role-tools-heading', 'Web Search').checked).toBe(true);
    expect(checkbox('role-tools-heading', 'Calculator').checked).toBe(false);
    expect(checkbox('role-models-heading', 'Nova Micro').checked).toBe(true);
    expect(checkbox('role-admin-heading', 'Cost Analytics').checked).toBe(true);
    expect(checkbox('role-admin-heading', 'Users').checked).toBe(false);
  });

  it('never offers the role being edited as its own parent', async () => {
    await mount('analyst');
    expect(rowNames('role-inherits-heading')).toEqual(['Basic User', 'Power User']);
  });

  it('groups admin areas in the registry’s own order, like the admin nav', async () => {
    await mount('new');
    const groups = [...picker('role-admin-heading').querySelectorAll('p[id*="-group-"]')].map((p) =>
      p.textContent?.trim(),
    );
    expect(groups).toEqual(['Usage & Spend', 'Identity & Access']);
  });

  it('locks a non-delegable admin scope and says why in visible text', async () => {
    await mount('new');
    expect(checkbox('role-admin-heading', 'Roles').disabled).toBe(true);
    expect(picker('role-admin-heading').textContent).toContain('System admins only');
    // "Select all" adds only what can be granted.
    const selectAll = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.getAttribute('aria-label') === 'Select all 2 shown admin areas',
    );
    expect(selectAll).toBeTruthy();
    selectAll!.click();
    fixture.detectChanges();
    expect(fixture.componentInstance.roleForm.controls.grantedAdminScopes.value).toEqual([
      'admin.costs',
      'admin.users',
    ]);
  });

  it('tags retiring tools and disabled models without blocking the grant', async () => {
    await mount('new');
    expect(picker('role-tools-heading').textContent).toContain('retiring');
    expect(picker('role-models-heading').textContent).toContain('disabled');
    const old = checkbox('role-tools-heading', 'Old Search');
    expect(old.disabled).toBe(false);
    click(old);
    expect(fixture.componentInstance.roleForm.controls.grantedTools.value).toEqual(['old_search']);
  });

  it('a toggle edits only its own control, and keeps ids the picker has no row for', async () => {
    await mount('analyst', role({ grantedTools: ['retired_tool', 'web_search'], inheritsFrom: ['disabled_parent'] }));
    const form = fixture.componentInstance.roleForm.controls;

    click(checkbox('role-tools-heading', 'Calculator'));
    click(checkbox('role-tools-heading', 'Web Search'));
    click(checkbox('role-inherits-heading', 'Power User'));

    expect(form.grantedTools.value).toEqual(['retired_tool', 'calculator']);
    expect(form.inheritsFrom.value).toEqual(['disabled_parent', 'power_user']);
    expect(form.grantedModels.value).toEqual([]);
    expect(form.grantedTools.dirty).toBe(true);
  });

  it('the wildcard hides the tool list, and clearing it brings back an empty one', async () => {
    await mount('new');
    const all = el.querySelector<HTMLInputElement>('#grantAllTools')!;
    click(checkbox('role-tools-heading', 'Calculator'));

    click(all);
    expect(fixture.componentInstance.roleForm.controls.grantedTools.value).toEqual(['*']);
    expect(el.querySelector('[aria-labelledby="role-tools-heading"]')).toBeNull();
    expect(el.querySelectorAll('app-tool-selector')).toHaveLength(3);

    click(all);
    expect(fixture.componentInstance.roleForm.controls.grantedTools.value).toEqual([]);
    expect(checkbox('role-tools-heading', 'Calculator').checked).toBe(false);
  });

  it('a loaded wildcard grant shows only the checkbox, for models too', async () => {
    await mount('analyst', role({ grantedModels: ['*'] }));
    expect(el.querySelector<HTMLInputElement>('#grantAllModels')!.checked).toBe(true);
    expect(el.querySelector('[aria-labelledby="role-models-heading"]')).toBeNull();
  });

  it('saves the edited arrays in the unchanged update request', async () => {
    await mount('analyst', role({ grantedTools: ['web_search'], grantedAdminScopes: ['admin.users'] }));
    click(checkbox('role-admin-heading', 'Cost Analytics'));
    click(checkbox('role-models-heading', 'Claude Haiku 4.5'));
    click(checkbox('role-inherits-heading', 'Basic User'));

    await fixture.componentInstance.onSubmit();

    expect(service.updateRole).toHaveBeenCalledWith('analyst', {
      displayName: 'Analyst',
      description: 'Reads reports',
      jwtRoleMappings: ['Analysts'],
      inheritsFrom: ['basic_user'],
      grantedTools: ['web_search'],
      grantedModels: ['us.anthropic.claude-haiku-4-5'],
      grantedAdminScopes: ['admin.users', 'admin.costs'],
      priority: 10,
      enabled: true,
    });
  });
});
