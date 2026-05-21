import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AuthProvidersService } from './auth-providers.service';
import { ConfigService } from '../../../services/config.service';
describe('AuthProvidersService', () => {
  let service: AuthProvidersService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AuthProvidersService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AuthProvidersService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should fetch providers', async () => {
    const mockResponse = { providers: [], total: 0 };
    const promise = service.fetchProviders();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/auth-providers/').flush(mockResponse);
    });
    expect(await promise).toEqual(mockResponse);
  });

  it('should fetch provider by id', async () => {
    const mockProvider = { provider_id: '1', name: 'Test Provider' };
    const promise = service.fetchProvider('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/auth-providers/1').flush(mockProvider);
    });
    expect(await promise).toEqual(mockProvider);
  });

  it('should create provider', async () => {
    const providerData = { name: 'New Provider' } as any;
    const mockProvider = { provider_id: '1', name: 'New Provider' };
    const promise = service.createProvider(providerData);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/auth-providers/').flush(mockProvider);
    });
    expect(await promise).toEqual(mockProvider);
  });

  it('should update provider', async () => {
    const updates = { name: 'Updated Provider' } as any;
    const mockProvider = { provider_id: '1', name: 'Updated Provider' };
    const promise = service.updateProvider('1', updates);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/auth-providers/1').flush(mockProvider);
    });
    expect(await promise).toEqual(mockProvider);
  });

  it('should delete provider', async () => {
    const promise = service.deleteProvider('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/auth-providers/1').flush(null);
    });
    await promise;
  });
});