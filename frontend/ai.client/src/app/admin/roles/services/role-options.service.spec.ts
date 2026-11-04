import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { RoleOptionsService } from './role-options.service';
import { AppRolesService } from './app-roles.service';
import { ConfigService } from '../../../services/config.service';
import { AppRoleOptionListResponse } from '../models/app-role.model';

const PICKER_URL = 'http://localhost:8000/admin/roles/picker-options';

const options: AppRoleOptionListResponse = {
  roles: [
    { roleId: 'models_admin', displayName: 'Models Admin', description: '', enabled: true },
    { roleId: 'retired', displayName: 'Retired', description: '', enabled: false },
    { roleId: 'faculty', displayName: 'Faculty', description: 'Teaching staff', enabled: true },
  ],
  total: 3,
};

describe('RoleOptionsService', () => {
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  async function loaded(): Promise<RoleOptionsService> {
    const service = TestBed.inject(RoleOptionsService);
    await vi.waitFor(() => {
      TestBed.tick();
      httpMock.expectOne(PICKER_URL).flush(options);
    });
    await vi.waitFor(() => {
      TestBed.tick();
      expect(service.rolesResource.hasValue()).toBe(true);
    });
    return service;
  }

  // The full list is system_admin-only; a delegated admin's pickers must never ask for it.
  it('reads the picker list, never the full role list', async () => {
    const service = await loaded();
    httpMock.expectNone('http://localhost:8000/admin/roles/');
    expect(service.getRoles().map(r => r.roleId)).toEqual(['models_admin', 'retired', 'faculty']);
  });

  it('keeps the server order and filters to enabled roles on request', async () => {
    const service = await loaded();
    expect(service.getEnabledRoles().map(r => r.roleId)).toEqual(['models_admin', 'faculty']);
  });

  it('finds a role by id for list pages that name a grant', async () => {
    const service = await loaded();
    expect(service.getRoleById('faculty')?.displayName).toBe('Faculty');
    expect(service.getRoleById('nope')).toBeUndefined();
  });

  it('fetches on demand for dialogs that load their own copy', async () => {
    const service = TestBed.inject(RoleOptionsService);
    const promise = service.fetchRoles();
    httpMock.match(PICKER_URL).forEach(req => req.flush(options));
    expect(await promise).toEqual(options);
  });

  // A system admin who just created a role should see it in the next picker they open.
  it('is reloaded when a role is created', async () => {
    await loaded();
    const roles = TestBed.inject(AppRolesService);
    const created = roles.createRole({ roleId: 'staff' } as never);
    httpMock.expectOne(req => req.method === 'POST').flush({ roleId: 'staff' });
    await created;
    await vi.waitFor(() => {
      TestBed.tick();
      httpMock.expectOne(PICKER_URL);
    });
  });
});
