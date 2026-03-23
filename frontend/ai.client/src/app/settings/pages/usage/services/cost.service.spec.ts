import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { CostService } from './cost.service';
import { ConfigService } from '../../../../services/config.service';
import { AuthService } from '../../../../auth/auth.service';

describe('CostService', () => {
  let service: CostService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        CostService,
        { provide: AuthService, useValue: { ensureAuthenticated: vi.fn().mockResolvedValue(undefined) } },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(CostService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true); // discard pending requests
    TestBed.resetTestingModule();
  });

  it('should fetch cost summary', async () => {
    const mockResponse = { totalCost: 10.50, totalRequests: 100 };
    
    const promise = service.fetchCostSummary();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/costs/summary').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should fetch cost summary with period', async () => {
    const mockResponse = { totalCost: 5.25, totalRequests: 50 };
    
    const promise = service.fetchCostSummary('2025-01');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/costs/summary?period=2025-01').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should fetch detailed report', async () => {
    const mockResponse = { totalCost: 15.75, totalRequests: 150 };
    
    const promise = service.fetchDetailedReport('2025-01-01', '2025-01-31');
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/costs/detailed-report?start_date=2025-01-01&end_date=2025-01-31').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get cost summary for month', async () => {
    const mockResponse = { totalCost: 8.00, totalRequests: 80 };
    
    const promise = service.getCostSummaryForMonth(2025, 1);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/costs/summary?period=2025-01').flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get cost summary for last N days', async () => {
    const mockResponse = { totalCost: 3.25, totalRequests: 30 };
    
    const promise = service.getCostSummaryForLastNDays(7);
    await vi.waitFor(() => {
      const req = httpMock.expectOne(r => r.url.includes('/costs/detailed-report'));
      req.flush(mockResponse);
    });
    
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });
});