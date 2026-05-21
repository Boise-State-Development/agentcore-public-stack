import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ConnectorsService } from './connectors.service';
import { ConfigService } from '../../../services/config.service';
describe('ConnectorsService', () => {
  let service: ConnectorsService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        ConnectorsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ConnectorsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({});
    });
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

  it('should fetch file-source adapters without key translation', async () => {
    const mockResponse = {
      adapters: [
        {
          key: 'google-drive',
          displayName: 'Google Drive',
          icon: 'google-drive',
          compatibleProviderTypes: ['google'],
          requiredScopes: ['https://www.googleapis.com/auth/drive.readonly'],
        },
      ],
    };
    const promise = service.fetchFileSourceAdapters();
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/file-source-adapters/')
        .flush(mockResponse);
    });
    // The endpoint already serializes camelCase — response passes through as-is.
    expect(await promise).toEqual(mockResponse);
  });
});
