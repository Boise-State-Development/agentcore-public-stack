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

  it('should get session cost anatomy', () => {
    const mockAnatomy = {
      sessionId: 'sess-1',
      calls: [],
      totalCost: 1.23,
      totalCacheReadTokens: 1000,
      totalCacheWriteTokens: 500,
      avoidableMissCount: 0,
      wastedUsd: 0,
      cacheEfficiency: 2 / 3,
    };

    service.getSessionCostAnatomy('sess-1').subscribe(anatomy => {
      expect(anatomy).toEqual(mockAnatomy);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/sessions/sess-1/calls');
    expect(req.request.method).toBe('GET');
    req.flush(mockAnatomy);
  });

  it('should get a user’s sessions with scope and sort params', () => {
    const mock = { userId: 'u1', sessions: [], total: 0, unknownCostCount: 0 };

    service.getUserSessions('u1', { allTime: true, sort: 'recent', limit: 50 }).subscribe(resp => {
      expect(resp).toEqual(mock);
    });

    const req = httpMock.expectOne(
      'http://localhost:8000/admin/costs/users/u1/sessions?allTime=true&sort=recent&limit=50'
    );
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });

  it('should URL-encode the user id and send period when scoped', () => {
    service.getUserSessions('a/b', { period: '2026-09' }).subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/users/a%2Fb/sessions?period=2026-09');
    expect(req.request.method).toBe('GET');
    req.flush({ userId: 'a/b', sessions: [], total: 0, unknownCostCount: 0 });
  });

  it('should get a session profile', () => {
    const mock = { sessionId: 'sess-1', diagnoses: [] };

    service.getSessionProfile('sess-1').subscribe(profile => {
      expect(profile).toEqual(mock);
    });

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/sessions/sess-1/profile');
    expect(req.request.method).toBe('GET');
    req.flush(mock);
  });

  it('should URL-encode the session id in the anatomy request', () => {
    service.getSessionCostAnatomy('a/b c').subscribe();

    const req = httpMock.expectOne('http://localhost:8000/admin/costs/sessions/a%2Fb%20c/calls');
    expect(req.request.method).toBe('GET');
    req.flush({ sessionId: 'a/b c', calls: [] });
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