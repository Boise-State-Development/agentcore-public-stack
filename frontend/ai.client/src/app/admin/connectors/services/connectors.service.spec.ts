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
    const mockResponse = { providers: [], total: 0 };
    const promise = service.fetchConnectors();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/oauth-providers/').flush(mockResponse);
    });
    expect(await promise).toEqual(mockResponse);
  });

  it('should fetch connector by id', async () => {
    const mockConnector = { provider_id: '1', name: 'Test Connector' };
    const promise = service.fetchConnector('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/oauth-providers/1').flush(mockConnector);
    });
    expect(await promise).toEqual({ providerId: '1', name: 'Test Connector' });
  });

  it('should create connector', async () => {
    const data = { name: 'New Connector' } as any;
    const mockConnector = { provider_id: '1', name: 'New Connector' };
    const promise = service.createConnector(data);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/oauth-providers/').flush(mockConnector);
    });
    expect(await promise).toEqual({ providerId: '1', name: 'New Connector' });
  });

  it('should update connector', async () => {
    const updates = { name: 'Updated Connector' } as any;
    const mockConnector = { provider_id: '1', name: 'Updated Connector' };
    const promise = service.updateConnector('1', updates);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/oauth-providers/1').flush(mockConnector);
    });
    expect(await promise).toEqual({ providerId: '1', name: 'Updated Connector' });
  });

  it('should delete connector', async () => {
    const promise = service.deleteConnector('1');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/oauth-providers/1').flush(null);
    });
    await promise;
  });
});
