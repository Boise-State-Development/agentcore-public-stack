import { TestBed } from '@angular/core/testing';
import { UsageSettingsPage } from './usage-settings.page';
import { CostService } from './services/cost.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import type { QuotaStatus } from '../../../services/quota/quota-status.model';
import type { UserCostSummary } from './models/cost-summary.model';

function summary(totalCost: number): UserCostSummary {
  return {
    userId: 'u1',
    periodStart: '2026-09-01',
    periodEnd: '2026-09-30',
    totalCost,
    models: [],
    totalRequests: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    totalCacheSavings: 0,
  };
}

function quota(overrides: Partial<QuotaStatus> = {}): QuotaStatus {
  return {
    configured: true,
    unlimited: false,
    tierName: 'Standard',
    matchedBy: 'jwt_role:Faculty',
    monthlyLimit: 10,
    currentUsage: 2.5,
    remaining: 7.5,
    usagePercentage: 25,
    periodType: 'monthly',
    resetInfo: 'Quota resets in 12 day(s)',
    hasActiveOverride: false,
    ...overrides,
  };
}

describe('UsageSettingsPage quota bar', () => {
  let costTotal: number;
  let quotaValue: QuotaStatus | undefined;

  function build() {
    const costStub = {
      currentMonthSummary: {
        value: () => summary(costTotal),
        isLoading: () => false,
        error: () => null,
      },
      reloadCurrentMonthSummary: () => {},
    };
    const quotaStub = {
      status: { value: () => quotaValue },
      reload: () => {},
    };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: CostService, useValue: costStub },
        { provide: QuotaStatusService, useValue: quotaStub },
      ],
    });
    const fixture = TestBed.createComponent(UsageSettingsPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('shows a bar with percentage against the monthly limit', () => {
    costTotal = 2.5;
    quotaValue = quota({ monthlyLimit: 10 });
    const c = build();
    const bar = c.quotaBar();
    expect(bar).not.toBeNull();
    expect(bar!.limit).toBe(10);
    expect(bar!.pct).toBe(25);
    expect(bar!.remaining).toBe(7.5);
  });

  it('caps the bar at 100% when spend exceeds the limit', () => {
    costTotal = 15;
    quotaValue = quota({ monthlyLimit: 10 });
    const c = build();
    expect(c.quotaBar()!.pct).toBe(100);
    expect(c.quotaBar()!.remaining).toBe(0);
  });

  it('hides the bar and reports unlimited for an unlimited tier', () => {
    costTotal = 2.5;
    quotaValue = quota({ unlimited: true, monthlyLimit: null });
    const c = build();
    expect(c.quotaBar()).toBeNull();
    expect(c.quotaUnlimited()).toBe(true);
  });

  it('hides the bar when no quota is configured', () => {
    costTotal = 2.5;
    quotaValue = quota({ configured: false, monthlyLimit: null });
    const c = build();
    expect(c.quotaBar()).toBeNull();
    expect(c.quotaUnlimited()).toBe(false);
  });

  it('hides the bar while quota status is still loading', () => {
    costTotal = 2.5;
    quotaValue = undefined;
    const c = build();
    expect(c.quotaBar()).toBeNull();
  });
});
