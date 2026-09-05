import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { QuotaCardComponent } from './quota-card.component';
import type { FineTuningAccessResponse } from '../models/fine-tuning.models';

describe('QuotaCardComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent(access: FineTuningAccessResponse) {
    const fixture = TestBed.createComponent(QuotaCardComponent);
    fixture.componentRef.setInput('access', access);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  const baseAccess: FineTuningAccessResponse = {
    has_access: true,
    monthly_quota_usd: 10,
    current_month_usage_usd: 3,
    quota_period: '2026-03',
  };

  it('should compute used hours from access signal', () => {
    const component = createComponent(baseAccess);
    expect(component.usedUsd()).toBe(3);
  });

  it('should compute total hours from access signal', () => {
    const component = createComponent(baseAccess);
    expect(component.totalUsd()).toBe(10);
  });

  it('should compute remaining hours', () => {
    const component = createComponent(baseAccess);
    expect(component.remainingUsd()).toBe(7);
  });

  it('should not return negative remaining hours', () => {
    const component = createComponent({ ...baseAccess, current_month_usage_usd: 15 });
    expect(component.remainingUsd()).toBe(0);
  });

  it('should compute used percent correctly', () => {
    const component = createComponent(baseAccess); // 3/10 = 30%
    expect(component.usedPercent()).toBe(30);
  });

  it('should cap used percent at 100', () => {
    const component = createComponent({ ...baseAccess, current_month_usage_usd: 15 });
    expect(component.usedPercent()).toBe(100);
  });

  it('should return 0 percent when total is 0', () => {
    const component = createComponent({ ...baseAccess, monthly_quota_usd: 0 });
    expect(component.usedPercent()).toBe(0);
  });

  it('should return 0 percent when total is null', () => {
    const component = createComponent({ ...baseAccess, monthly_quota_usd: null });
    expect(component.usedPercent()).toBe(0);
  });

  it('should return info bar color for low usage (<70%)', () => {
    const component = createComponent(baseAccess); // 30%
    expect(component.barColor()).toBe('bg-state-info-500');
  });

  it('should return warning bar color for moderate usage (70-89%)', () => {
    const component = createComponent({ ...baseAccess, current_month_usage_usd: 7.5 }); // 75%
    expect(component.barColor()).toBe('bg-state-warning-500');
  });

  it('should return danger bar color for high usage (>=90%)', () => {
    const component = createComponent({ ...baseAccess, current_month_usage_usd: 9.5 }); // 95%
    expect(component.barColor()).toBe('bg-state-danger-500');
  });

  it('should handle null usage hours', () => {
    const component = createComponent({ ...baseAccess, current_month_usage_usd: null });
    expect(component.usedUsd()).toBe(0);
    expect(component.usedPercent()).toBe(0);
    expect(component.barColor()).toBe('bg-state-info-500');
  });
});
