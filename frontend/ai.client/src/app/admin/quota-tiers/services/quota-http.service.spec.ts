import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { QuotaHttpService } from './quota-http.service';
import { ConfigService } from '../../../services/config.service';

describe('QuotaHttpService', () => {
  let service: QuotaHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        QuotaHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(QuotaHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should get tiers', () => {
    const mockTiers = [{ id: '1', name: 'Basic' }];

    service.getTiers().subscribe(tiers => {
      expect(tiers).toEqual(mockTiers);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/quota/tiers?enabled_only=false');
    expect(req.request.method).toBe('GET');
    req.flush(mockTiers);
  });

  it('should create tier', () => {
    const mockTier = { id: '1', name: 'Basic' };
    const createData = { name: 'Basic' } as any;

    service.createTier(createData).subscribe(tier => {
      expect(tier).toEqual(mockTier);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/quota/tiers');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(createData);
    req.flush(mockTier);
  });

  it('should get assignments', () => {
    const mockAssignments = [{ id: '1', tierId: 'tier1' }];

    service.getAssignments().subscribe(assignments => {
      expect(assignments).toEqual(mockAssignments);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/quota/assignments?enabled_only=false');
    expect(req.request.method).toBe('GET');
    req.flush(mockAssignments);
  });

  it('should get user quota info', () => {
    const mockInfo = { userId: 'user1', quotaUsed: 100 };

    service.getUserQuotaInfo('user1').subscribe(info => {
      expect(info).toEqual(mockInfo);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/quota/users/user1');
    expect(req.request.method).toBe('GET');
    req.flush(mockInfo);
  });
});