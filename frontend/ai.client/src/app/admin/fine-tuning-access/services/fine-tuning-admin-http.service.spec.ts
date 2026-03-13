import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { FineTuningAdminHttpService } from './fine-tuning-admin-http.service';
import { ConfigService } from '../../../services/config.service';
import type { AccessListResponse, FineTuningGrant } from '../models/fine-tuning-access.models';

describe('FineTuningAdminHttpService', () => {
  let service: FineTuningAdminHttpService;
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
    service = TestBed.inject(FineTuningAdminHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    TestBed.resetTestingModule();
  });

  it('should list grants', () => {
    const mockResponse: AccessListResponse = {
      grants: [
        {
          email: 'user@example.com',
          granted_by: 'admin@example.com',
          granted_at: '2026-03-01T00:00:00Z',
          monthly_quota_hours: 10,
          current_month_usage_hours: 3,
          quota_period: '2026-03',
        },
      ],
      total_count: 1,
    };

    service.listGrants().subscribe((result) => {
      expect(result).toEqual(mockResponse);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/fine-tuning/access');
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should grant access', () => {
    const mockGrant: FineTuningGrant = {
      email: 'new@example.com',
      granted_by: 'admin@example.com',
      granted_at: '2026-03-01T00:00:00Z',
      monthly_quota_hours: 20,
      current_month_usage_hours: 0,
      quota_period: '2026-03',
    };

    service.grantAccess('new@example.com', 20).subscribe((result) => {
      expect(result).toEqual(mockGrant);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/fine-tuning/access');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ email: 'new@example.com', monthly_quota_hours: 20 });
    req.flush(mockGrant);
  });

  it('should update quota', () => {
    const mockGrant: FineTuningGrant = {
      email: 'user@example.com',
      granted_by: 'admin@example.com',
      granted_at: '2026-03-01T00:00:00Z',
      monthly_quota_hours: 50,
      current_month_usage_hours: 3,
      quota_period: '2026-03',
    };

    service.updateQuota('user@example.com', 50).subscribe((result) => {
      expect(result).toEqual(mockGrant);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/fine-tuning/access/user%40example.com');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ monthly_quota_hours: 50 });
    req.flush(mockGrant);
  });

  it('should revoke access', () => {
    service.revokeAccess('user@example.com').subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/fine-tuning/access/user%40example.com');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
  });

  it('should encode email with special characters in URL', () => {
    service.updateQuota('user+tag@example.com', 10).subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/fine-tuning/access/user%2Btag%40example.com');
    expect(req.request.method).toBe('PUT');
    req.flush({});
  });
});
