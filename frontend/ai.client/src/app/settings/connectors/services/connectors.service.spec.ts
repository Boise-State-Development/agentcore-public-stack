import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ConnectorsService } from './connectors.service';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';

describe('ConnectorsService', () => {
  let service: ConnectorsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        ConnectorsService,
        { provide: AuthService, useValue: { ensureAuthenticated: vi.fn().mockResolvedValue(undefined) } },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ConnectorsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should fetch connectors', async () => {
    const mockResponse = { connections: [{ provider_id: 'google', status: 'connected' }] };

    const connectorsPromise = service.fetchConnectors();

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/oauth/connections').flush(mockResponse);
    });

    const connectors = await connectorsPromise;
    expect(connectors.connectors[0].providerId).toBe('google');
  });

  it('should fetch providers', async () => {
    const mockResponse = { providers: [{ provider_id: 'google', name: 'Google' }], total: 1 };

    const providersPromise = service.fetchProviders();

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/oauth/providers').flush(mockResponse);
    });

    const providers = await providersPromise;
    expect(providers.providers[0].providerId).toBe('google');
    expect(providers.total).toBe(1);
  });

  it('should connect to provider', async () => {
    const mockResponse = { authorization_url: 'https://oauth.example.com/auth' };

    const connectPromise = service.connect('google');

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/oauth/connect/google').flush(mockResponse);
    });

    const authUrl = await connectPromise;
    expect(authUrl).toBe('https://oauth.example.com/auth');
  });

  it('should disconnect from provider', async () => {
    const disconnectPromise = service.disconnect('google');

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/oauth/connections/google').flush({});
    });

    await disconnectPromise;
  });
});
