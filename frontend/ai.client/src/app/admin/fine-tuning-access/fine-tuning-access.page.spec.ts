import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { FineTuningAccessPage } from './fine-tuning-access.page';
import { FineTuningAdminStateService } from './services/fine-tuning-admin-state.service';
import type { FineTuningGrant } from './models/fine-tuning-access.models';

const mockGrant: FineTuningGrant = {
  email: 'user@example.com',
  granted_by: 'admin@example.com',
  granted_at: '2026-03-01T00:00:00Z',
  monthly_quota_hours: 10,
  current_month_usage_hours: 3,
  quota_period: '2026-03',
};

function createMockState() {
  return {
    grants: signal<FineTuningGrant[]>([mockGrant]),
    loading: signal(false),
    error: signal<string | null>(null),
    showGrantForm: signal(false),
    grantCount: signal(1),
    hasError: signal(false),
    loadGrants: vi.fn().mockResolvedValue(undefined),
    grantAccess: vi.fn().mockResolvedValue(undefined),
    updateQuota: vi.fn().mockResolvedValue(undefined),
    revokeAccess: vi.fn().mockResolvedValue(undefined),
    toggleGrantForm: vi.fn(),
    clearError: vi.fn(),
  };
}

describe('FineTuningAccessPage', () => {
  let mockState: ReturnType<typeof createMockState>;

  beforeEach(() => {
    mockState = createMockState();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: FineTuningAdminStateService, useValue: mockState },
      ],
    });
    TestBed.overrideComponent(FineTuningAccessPage, {
      set: { template: '<div></div>' },
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(FineTuningAccessPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should load grants on init', () => {
    createComponent();
    expect(mockState.loadGrants).toHaveBeenCalled();
  });

  it('should submit grant with trimmed lowercase email', () => {
    const component = createComponent();
    component.newEmail = '  Admin@Example.COM  ';
    component.newQuota = 25;
    component.submitGrant();
    expect(mockState.grantAccess).toHaveBeenCalledWith('admin@example.com', 25);
    expect(component.newEmail).toBe('');
    expect(component.newQuota).toBe(10);
  });

  it('should not submit grant with empty email', () => {
    const component = createComponent();
    component.newEmail = '   ';
    component.submitGrant();
    expect(mockState.grantAccess).not.toHaveBeenCalled();
  });

  it('should start editing a grant', () => {
    const component = createComponent();
    component.startEdit('user@example.com', 10);
    expect(component.editingEmail()).toBe('user@example.com');
    expect(component.editQuota()).toBe(10);
    expect(component.confirmingRevoke()).toBeNull();
  });

  it('should cancel edit', () => {
    const component = createComponent();
    component.startEdit('user@example.com', 10);
    component.cancelEdit();
    expect(component.editingEmail()).toBeNull();
  });

  it('should submit quota update', () => {
    const component = createComponent();
    component.startEdit('user@example.com', 10);
    component.editQuota.set(50);
    component.submitQuotaUpdate();
    expect(mockState.updateQuota).toHaveBeenCalledWith('user@example.com', 50);
    expect(component.editingEmail()).toBeNull();
  });

  it('should not submit quota update when no email is editing', () => {
    const component = createComponent();
    component.submitQuotaUpdate();
    expect(mockState.updateQuota).not.toHaveBeenCalled();
  });

  it('should confirm revoke and clear editing', () => {
    const component = createComponent();
    component.startEdit('user@example.com', 10);
    component.confirmRevoke('user@example.com');
    expect(component.confirmingRevoke()).toBe('user@example.com');
    expect(component.editingEmail()).toBeNull();
  });

  it('should execute revoke', () => {
    const component = createComponent();
    component.confirmRevoke('user@example.com');
    component.executeRevoke('user@example.com');
    expect(mockState.revokeAccess).toHaveBeenCalledWith('user@example.com');
    expect(component.confirmingRevoke()).toBeNull();
  });

  it('should cancel revoke', () => {
    const component = createComponent();
    component.confirmRevoke('user@example.com');
    component.cancelRevoke();
    expect(component.confirmingRevoke()).toBeNull();
  });

  it('should format ISO date string', () => {
    const component = createComponent();
    // Use a midday time to avoid timezone boundary issues
    const formatted = component.formatDate('2026-03-15T12:00:00Z');
    expect(formatted).toContain('2026');
    expect(formatted).toContain('Mar');
  });

  it('should compute usage percent', () => {
    const component = createComponent();
    expect(component.usagePercent(3, 10)).toBe(30);
    expect(component.usagePercent(10, 10)).toBe(100);
    expect(component.usagePercent(15, 10)).toBe(100); // capped at 100
    expect(component.usagePercent(0, 0)).toBe(0); // zero quota
    expect(component.usagePercent(5, 0)).toBe(0); // zero quota
  });

  it('should return correct usage bar color', () => {
    const component = createComponent();
    expect(component.usageBarColor(3, 10)).toBe('bg-blue-500'); // 30%
    expect(component.usageBarColor(7, 10)).toBe('bg-amber-500'); // 70%
    expect(component.usageBarColor(8, 10)).toBe('bg-amber-500'); // 80%
    expect(component.usageBarColor(9, 10)).toBe('bg-red-500'); // 90%
    expect(component.usageBarColor(10, 10)).toBe('bg-red-500'); // 100%
  });
});
