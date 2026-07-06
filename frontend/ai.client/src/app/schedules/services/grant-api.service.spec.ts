import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { GrantApiService } from './grant-api.service';
import { ConfigService } from '../../services/config.service';

const BASE = 'http://localhost:8000/runs/grant';

describe('GrantApiService', () => {
  let service: GrantApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        GrantApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(GrantApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('fetches grant status', async () => {
    const status = { enabled: true, grantId: 'hlg-1' };
    const promise = firstValueFrom(service.status());

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('GET');
      req.flush(status);
    });

    expect(await promise).toEqual(status);
  });

  it('enables (creates/refreshes) the grant via POST', async () => {
    const status = { enabled: true, grantId: 'hlg-1' };
    const promise = firstValueFrom(service.enable());

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('POST');
      req.flush(status);
    });

    expect(await promise).toEqual(status);
  });

  it('revokes the grant via DELETE', async () => {
    const promise = firstValueFrom(service.revoke());

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('DELETE');
      req.flush({ revoked: true });
    });

    expect(await promise).toEqual({ revoked: true });
  });
});
