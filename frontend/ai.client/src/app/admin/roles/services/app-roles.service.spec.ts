import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AppRolesService } from './app-roles.service';
import { ConfigService } from '../../../services/config.service';
describe('AppRolesService', () => {
  let service: AppRolesService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AppRolesService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AppRolesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should fetch roles', async () => {
    const mockResponse = { roles: [], total: 0 };
    const promise = service.fetchRoles();
    httpMock.expectOne('http://localhost:8000/admin/roles/').flush(mockResponse);
    expect(await promise).toEqual(mockResponse);
  });

  it('should fetch role by id', async () => {
    const mockRole = { roleId: '1', name: 'Test Role' };
    const promise = service.fetchRole('1');
    httpMock.expectOne('http://localhost:8000/admin/roles/1').flush(mockRole);
    expect(await promise).toEqual(mockRole);
  });

  it('should create role', async () => {
    const roleData = { name: 'New Role' } as any;
    const mockRole = { roleId: '1', name: 'New Role' };
    const promise = service.createRole(roleData);
    httpMock.expectOne('http://localhost:8000/admin/roles/').flush(mockRole);
    expect(await promise).toEqual(mockRole);
  });

  it('should update role', async () => {
    const updates = { name: 'Updated Role' } as any;
    const mockRole = { roleId: '1', name: 'Updated Role' };
    const promise = service.updateRole('1', updates);
    httpMock.expectOne('http://localhost:8000/admin/roles/1').flush(mockRole);
    expect(await promise).toEqual(mockRole);
  });

  it('should delete role', async () => {
    const promise = service.deleteRole('1');
    httpMock.expectOne('http://localhost:8000/admin/roles/1').flush(null);
    await promise;
  });
});