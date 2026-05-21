import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AdminCostHttpService } from './admin-cost-http.service';
import { ConfigService } from '../../../services/config.service';

describe('AdminCostHttpService', () => {
  let service: AdminCostHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AdminCostHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AdminCostHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should get dashboard', () => {
    const mockDashboard = { systemSummary: { totalCost: 100 }, topUsers: [] };

    service.getDashboard().subscribe(dashboard => {
      expect(dashboard).toEqual(mockDashboard);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/dashboard');
    expect(req.request.method).toBe('GET');
    req.flush(mockDashboard);
  });

  it('should get top users', () => {
    const mockUsers = [{ userId: 'user1', totalCost: 50 }];

    service.getTopUsers({ limit: 10 }).subscribe(users => {
      expect(users).toEqual(mockUsers);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/top-users?limit=10');
    expect(req.request.method).toBe('GET');
    req.flush(mockUsers);
  });

  it('should get system summary', () => {
    const mockSummary = { totalCost: 100, totalRequests: 50 };

    service.getSystemSummary('2024-01').subscribe(summary => {
      expect(summary).toEqual(mockSummary);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/system-summary?periodType=monthly&period=2024-01');
    expect(req.request.method).toBe('GET');
    req.flush(mockSummary);
  });

  it('should export data', () => {
    const mockBlob = new Blob(['csv data'], { type: 'text/csv' });

    service.exportData('2024-01', 'csv').subscribe(blob => {
      expect(blob).toEqual(mockBlob);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/export?format=csv&period=2024-01');
    expect(req.request.method).toBe('GET');
    req.flush(mockBlob);
  });
});