import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { FineTuningAdminStateService } from './fine-tuning-admin-state.service';
import { FineTuningAdminHttpService } from './fine-tuning-admin-http.service';
import type { FineTuningGrant, AccessListResponse } from '../models/fine-tuning-access.models';

const mockGrant: FineTuningGrant = {
  email: 'user@example.com',
  granted_by: 'admin@example.com',
  granted_at: '2026-03-01T00:00:00Z',
  monthly_quota_hours: 10,
  current_month_usage_hours: 3,
  quota_period: '2026-03',
};

const mockListResponse: AccessListResponse = {
  grants: [mockGrant],
  total_count: 1,
};

function createMockHttp() {
  return {
    listGrants: vi.fn().mockReturnValue(of(mockListResponse)),
    grantAccess: vi.fn().mockReturnValue(of(mockGrant)),
    updateQuota: vi.fn().mockReturnValue(of(mockGrant)),
    revokeAccess: vi.fn().mockReturnValue(of(undefined)),
  };
}

describe('FineTuningAdminStateService', () => {
  let service: FineTuningAdminStateService;
  let mockHttp: ReturnType<typeof createMockHttp>;

  beforeEach(() => {
    mockHttp = createMockHttp();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FineTuningAdminHttpService, useValue: mockHttp },
      ],
    });
    service = TestBed.inject(FineTuningAdminStateService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should have correct initial state', () => {
    expect(service.grants()).toEqual([]);
    expect(service.loading()).toBe(false);
    expect(service.error()).toBeNull();
    expect(service.showGrantForm()).toBe(false);
    expect(service.grantCount()).toBe(0);
    expect(service.hasError()).toBe(false);
  });

  it('should load grants successfully', async () => {
    await service.loadGrants();
    expect(service.grants()).toEqual([mockGrant]);
    expect(service.grantCount()).toBe(1);
    expect(service.loading()).toBe(false);
    expect(service.error()).toBeNull();
  });

  it('should set error on load grants failure', async () => {
    mockHttp.listGrants.mockReturnValueOnce(throwError(() => new Error('Network error')));
    await service.loadGrants();
    expect(service.error()).toBe('Network error');
    expect(service.hasError()).toBe(true);
    expect(service.loading()).toBe(false);
  });

  it('should grant access and refresh list', async () => {
    await service.grantAccess('new@example.com', 20);
    expect(mockHttp.grantAccess).toHaveBeenCalledWith('new@example.com', 20);
    expect(mockHttp.listGrants).toHaveBeenCalled();
    expect(service.showGrantForm()).toBe(false);
  });

  it('should set error on grant access failure', async () => {
    mockHttp.grantAccess.mockReturnValueOnce(throwError(() => new Error('Grant failed')));
    await service.grantAccess('fail@example.com', 10);
    expect(service.error()).toBe('Grant failed');
    expect(service.loading()).toBe(false);
  });

  it('should update quota and refresh list', async () => {
    await service.updateQuota('user@example.com', 50);
    expect(mockHttp.updateQuota).toHaveBeenCalledWith('user@example.com', 50);
    expect(mockHttp.listGrants).toHaveBeenCalled();
  });

  it('should set error on update quota failure', async () => {
    mockHttp.updateQuota.mockReturnValueOnce(throwError(() => new Error('Update failed')));
    await service.updateQuota('user@example.com', 50);
    expect(service.error()).toBe('Update failed');
    expect(service.loading()).toBe(false);
  });

  it('should revoke access and refresh list', async () => {
    await service.revokeAccess('user@example.com');
    expect(mockHttp.revokeAccess).toHaveBeenCalledWith('user@example.com');
    expect(mockHttp.listGrants).toHaveBeenCalled();
  });

  it('should set error on revoke access failure', async () => {
    mockHttp.revokeAccess.mockReturnValueOnce(throwError(() => new Error('Revoke failed')));
    await service.revokeAccess('user@example.com');
    expect(service.error()).toBe('Revoke failed');
    expect(service.loading()).toBe(false);
  });

  it('should toggle grant form visibility', () => {
    expect(service.showGrantForm()).toBe(false);
    service.toggleGrantForm();
    expect(service.showGrantForm()).toBe(true);
    service.toggleGrantForm();
    expect(service.showGrantForm()).toBe(false);
  });

  it('should clear error', () => {
    service.error.set('some error');
    expect(service.hasError()).toBe(true);
    service.clearError();
    expect(service.error()).toBeNull();
    expect(service.hasError()).toBe(false);
  });

  it('should set generic error message for non-Error throws', async () => {
    mockHttp.listGrants.mockReturnValueOnce(throwError(() => 'unknown'));
    await service.loadGrants();
    expect(service.error()).toBe('Failed to load access grants');
  });
});
