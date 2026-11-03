import { TestBed } from '@angular/core/testing';
import { UsageSettingsPage } from './usage-settings.page';
import { CostService } from './services/cost.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import type { QuotaStatus } from '../../../services/quota/quota-status.model';
import type { UserCostSummary } from './models/cost-summary.model';
import { FEATURES } from '../../../services/features';
import { environment } from '../../../../environments/environment';
import { QuotaRequestService } from '../../../services/quota/quota-request.service';
import { signal } from '@angular/core';

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

describe('UsageSettingsPage quota requests', () => {
  const requests = signal<unknown[] | null>(null);
  const loadRequests = vi.fn().mockResolvedValue(undefined);
  const reload = vi.fn();

  function build(quotaRequests: boolean, status: QuotaStatus) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: CostService,
          useValue: {
            currentMonthSummary: { value: () => summary(status.currentUsage), isLoading: () => false, error: () => null },
            reloadCurrentMonthSummary: () => {},
          },
        },
        { provide: QuotaStatusService, useValue: { status: { value: () => status }, reload } },
        { provide: QuotaRequestService, useValue: { requests, requestsLoadError: () => null, loadRequests } },
        { provide: FEATURES, useValue: { ...environment.features, quotaRequests } },
      ],
    });
    const fixture = TestBed.createComponent(UsageSettingsPage);
    fixture.detectChanges();
    return { component: fixture.componentInstance, el: fixture.nativeElement as HTMLElement };
  }

  const extended = quota({
    monthlyLimit: 50, baseLimit: 30, effectiveLimit: 50, currentUsage: 31, remaining: 19, usagePercentage: 62,
    extensions: [{ amount: 20, validUntil: '2026-11-30T23:59:59+00:00', fundingMode: 'central', standing: false }],
  });

  beforeEach(() => {
    vi.clearAllMocks();
    requests.set([
      {
        requestId: 'r1', status: 'approved', kind: 'one_time', requestedAmount: '50', requestedUntil: '2026-11-30',
        justification: 'x', createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z',
        decision: { decidedAt: '2026-10-31T00:00:00Z', outcome: 'approved', amount: '20', validUntil: '2026-11-30', note: 'Enjoy' },
      },
    ]);
  });
  afterEach(() => TestBed.resetTestingModule());

  it('splits the bar into base and extensions, with the split in words', () => {
    const { component, el } = build(true, extended);
    const split = component.quotaSplit()!;
    expect(split.basePct).toBe(60);
    expect(el.querySelector('[data-testid="quota-split-legend"]')!.textContent!.replace(/\s+/g, ' ').trim())
      .toBe('$30 central + $20 additional through Nov 30');
  });

  it('says an approval ended early once its extension was revoked', () => {
    requests.set([
      {
        requestId: 'r1', status: 'approved', kind: 'one_time', requestedAmount: '50', requestedUntil: '2026-11-30',
        justification: 'x', createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z',
        decision: {
          decidedAt: '2026-10-31T00:00:00Z', outcome: 'approved', amount: '20', validUntil: '2026-11-30',
          extensionRevokedAt: '2026-11-05T12:00:00Z',
        },
      },
      {
        requestId: 'r2', status: 'approved', kind: 'one_time', requestedAmount: '10', requestedUntil: '2026-11-30',
        justification: 'x', createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z',
        decision: {
          decidedAt: '2026-10-31T00:00:00Z', outcome: 'approved', amount: '10', validUntil: '2026-11-30',
          extensionRevokedAt: '2026-11-05T12:00:00Z',
        },
      },
    ]);
    const { el } = build(true, extended);
    const text = el.querySelector('[data-testid="your-quota-requests"]')!.textContent!.replace(/\s+/g, ' ');
    expect(text).toContain('You asked for $50; $20 was approved. It was ended early on Nov 5, 2026.');
    expect(text).toContain('Approved, then ended early on Nov 5, 2026.');
    expect(text).not.toContain('through Nov 30, 2026.');
  });

  it('lists the user’s requests, saying when a different amount was approved', () => {
    const { el } = build(true, extended);
    expect(loadRequests).toHaveBeenCalled();
    expect(reload).toHaveBeenCalled();
    const list = el.querySelector('[data-testid="your-quota-requests"]')!;
    expect(list.textContent).toContain('$50 per month');
    expect(list.textContent).toContain('You asked for $50; $20 was approved through Nov 30, 2026.');
    expect(list.textContent).toContain('Enjoy');
    expect(el.textContent).toContain('Request more quota');
  });

  it('offers no request surface while the feature is off', () => {
    const { el } = build(false, extended);
    expect(loadRequests).not.toHaveBeenCalled();
    expect(el.querySelector('[data-testid="your-quota-requests"]')).toBeNull();
    expect(el.textContent).not.toContain('Request more quota');
  });
});
