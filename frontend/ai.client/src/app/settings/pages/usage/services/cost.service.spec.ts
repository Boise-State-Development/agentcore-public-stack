import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TestRequest } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { CostService } from './cost.service';
import { ConfigService } from '../../../../services/config.service';
describe('CostService', () => {
  let service: CostService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        CostService,
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

  // The CostService exposes a `resource()` field that auto-fires its loader on a
  // microtask. Under the vitest worker pool, a sibling spec can provoke the resource
  // graph to tick, producing an extra GET /costs/summary that lands in this spec's
  // HttpTestingController. `expectOne` then sees 2 requests and the assertion flakes.
  // Match by URL predicate and flush every match so the field-initializer request
  // is absorbed alongside the one our `it` actually triggered.
  const flushAll = <T extends object>(predicate: (url: string) => boolean, body: T): TestRequest[] => {
    const reqs = httpMock.match(r => predicate(r.url));
    expect(reqs.length).toBeGreaterThan(0);
    reqs.forEach(r => r.flush(body));
    return reqs;
  };

  it('should fetch cost summary', async () => {
    const mockResponse = { totalCost: 10.50, totalRequests: 100 };

    const promise = service.fetchCostSummary();
    await vi.waitFor(() => {
      flushAll(url => url === 'http://localhost:8000/costs/summary', mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should fetch cost summary with period', async () => {
    const mockResponse = { totalCost: 5.25, totalRequests: 50 };

    const promise = service.fetchCostSummary('2025-01');
    await vi.waitFor(() => {
      flushAll(url => url.endsWith('/costs/summary?period=2025-01'), mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should fetch detailed report', async () => {
    const mockResponse = { totalCost: 15.75, totalRequests: 150 };

    const promise = service.fetchDetailedReport('2025-01-01', '2025-01-31');
    await vi.waitFor(() => {
      flushAll(
        url => url.endsWith('/costs/detailed-report?start_date=2025-01-01&end_date=2025-01-31'),
        mockResponse,
      );
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get cost summary for month', async () => {
    const mockResponse = { totalCost: 8.00, totalRequests: 80 };

    const promise = service.getCostSummaryForMonth(2025, 1);
    await vi.waitFor(() => {
      flushAll(url => url.endsWith('/costs/summary?period=2025-01'), mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('should get cost summary for last N days', async () => {
    const mockResponse = { totalCost: 3.25, totalRequests: 30 };

    const promise = service.getCostSummaryForLastNDays(7);
    await vi.waitFor(() => {
      flushAll(url => url.includes('/costs/detailed-report'), mockResponse);
    });

    const result = await promise;
    expect(result).toEqual(mockResponse);
  });
});
