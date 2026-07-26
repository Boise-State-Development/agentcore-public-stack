import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { AdminCostStateService } from './admin-cost-state.service';
import { AdminCostHttpService } from './admin-cost-http.service';
import { of, throwError } from 'rxjs';

describe('AdminCostStateService', () => {
  let service: AdminCostStateService;
  let httpService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    
    const httpServiceMock = {
      getDashboard: vi.fn(),
      getTopUsers: vi.fn(),
      getSystemSummary: vi.fn(),
      getTrends: vi.fn(),
      exportData: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        AdminCostStateService,
        { provide: AdminCostHttpService, useValue: httpServiceMock },
      ],
    });

    service = TestBed.inject(AdminCostStateService);
    httpService = TestBed.inject(AdminCostHttpService);
  });

  afterEach(() => {
    // The export test spies on `document.createElement` and on
    // `document.body.appendChild`/`removeChild`. Left in place they follow
    // every spec that later shares this worker — `createElement` would hand
    // back a plain object instead of an Element.
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should load dashboard', async () => {
    const mockDashboard = {
      currentPeriod: { period: '2024-01', totalCost: 100 },
      topUsers: [],
      modelUsage: [],
    };
    httpService.getDashboard.mockReturnValue(of(mockDashboard));

    await service.loadDashboard();

    expect(service.dashboard()).toEqual(mockDashboard);
    expect(service.loading()).toBe(false);
  });

  it('should load top users', async () => {
    const mockUsers = [{ userId: '1', totalCost: 50 }];
    httpService.getTopUsers.mockReturnValue(of(mockUsers));

    await service.loadTopUsers();

    expect(service.topUsers()).toEqual(mockUsers);
    expect(service.loadingTopUsers()).toBe(false);
  });

  it('should load system summary', async () => {
    const mockSummary = { period: '2024-01', totalCost: 200 };
    httpService.getSystemSummary.mockReturnValue(of(mockSummary));

    await service.loadSystemSummary();

    expect(service.systemSummary()).toEqual(mockSummary);
    expect(service.loading()).toBe(false);
  });

  it('should load trends', async () => {
    const mockTrends = [{ date: '2024-01-01', totalCost: 10 }];
    httpService.getTrends.mockReturnValue(of(mockTrends));

    await service.loadTrends({ period: '2024-01' } as any);

    expect(service.trends()).toEqual(mockTrends);
    expect(service.loadingTrends()).toBe(false);
  });

  it('should reset state', () => {
    service.reset();

    expect(service.dashboard()).toBeNull();
    expect(service.topUsers()).toEqual([]);
    expect(service.error()).toBeNull();
  });

  it('should export data', async () => {
    const mockBlob = new Blob(['data']);
    httpService.exportData.mockReturnValue(of(mockBlob));
    
    const mockUrl = 'blob:url';
    const originalURL = globalThis.URL;
    const mockCreateObjectURL = vi.fn().mockReturnValue(mockUrl);
    const mockRevokeObjectURL = vi.fn();
    const mockClick = vi.fn();
    const mockElement = { href: '', download: '', click: mockClick };
    
    // Override only the static methods, preserve the constructor
    (globalThis.URL as any).createObjectURL = mockCreateObjectURL;
    (globalThis.URL as any).revokeObjectURL = mockRevokeObjectURL;
    vi.spyOn(document, 'createElement').mockReturnValue(mockElement as any);
    vi.spyOn(document.body, 'appendChild').mockImplementation(() => mockElement as any);
    vi.spyOn(document.body, 'removeChild').mockImplementation(() => mockElement as any);
    
    await service.exportData();
    
    expect(httpService.exportData).toHaveBeenCalled();
    expect(mockCreateObjectURL).toHaveBeenCalledWith(mockBlob);
    expect(mockElement.href).toBe(mockUrl);
    expect(mockClick).toHaveBeenCalled();
    expect(mockRevokeObjectURL).toHaveBeenCalledWith(mockUrl);
    
    // Restore
    globalThis.URL = originalURL;
  });

  it('should set period', () => {
    service.setPeriod('2024-01');
    expect(service.selectedPeriod()).toBe('2024-01');
  });

  it('should clear error', () => {
    service.error.set('test error');
    service.clearError();
    expect(service.error()).toBeNull();
  });

  it('should compute totalCost', () => {
    service.systemSummary.set({ totalCost: 150 } as any);
    expect(service.totalCost()).toBe(150);
    
    service.systemSummary.set(null);
    expect(service.totalCost()).toBe(0);
  });

  it('should compute totalRequests', () => {
    service.systemSummary.set({ totalRequests: 100 } as any);
    expect(service.totalRequests()).toBe(100);
    
    service.systemSummary.set(null);
    expect(service.totalRequests()).toBe(0);
  });

  it('should compute activeUsers', () => {
    service.systemSummary.set({ activeUsers: 25 } as any);
    expect(service.activeUsers()).toBe(25);
    
    service.systemSummary.set(null);
    expect(service.activeUsers()).toBe(0);
  });

  it('should compute cacheSavings', () => {
    service.systemSummary.set({ totalCacheSavings: 50 } as any);
    expect(service.cacheSavings()).toBe(50);
    
    service.systemSummary.set(null);
    expect(service.cacheSavings()).toBe(0);
  });

  it('should compute topUsersCount', () => {
    service.topUsers.set([{ userId: '1' } as any, { userId: '2' } as any]);
    expect(service.topUsersCount()).toBe(2);
    
    service.topUsers.set([]);
    expect(service.topUsersCount()).toBe(0);
  });

  it('should compute hasData', () => {
    service.dashboard.set({ currentPeriod: {} } as any);
    expect(service.hasData()).toBe(true);
    
    service.dashboard.set(null);
    expect(service.hasData()).toBe(false);
  });

  it('should compute hasTrends', () => {
    service.trends.set([{ date: '2024-01', totalCost: 10, totalRequests: 5, activeUsers: 2 }]);
    expect(service.hasTrends()).toBe(true);
    
    service.trends.set([]);
    expect(service.hasTrends()).toBe(false);
  });

  it('should load dashboard with demo period', async () => {
    service.setPeriod('demo');
    await service.loadDashboard();
    
    expect(httpService.getDashboard).not.toHaveBeenCalled();
    expect(service.dashboard()).toBeTruthy();
  });

  it('should handle dashboard error', async () => {
    httpService.getDashboard.mockReturnValue(throwError(() => new Error('API Error')));
    
    await expect(service.loadDashboard()).rejects.toThrow();
    
    expect(service.error()).toBe('API Error');
  });

  it('should handle top users error', async () => {
    httpService.getTopUsers.mockReturnValue(throwError(() => new Error('API Error')));
    
    await expect(service.loadTopUsers()).rejects.toThrow();
    
    expect(service.error()).toBe('API Error');
  });

  it('should load top users with demo period', async () => {
    service.setPeriod('demo');
    await service.loadTopUsers();
    
    expect(httpService.getTopUsers).not.toHaveBeenCalled();
    expect(service.topUsers()).toBeTruthy();
  });
});