import { TestBed } from '@angular/core/testing';
import { SessionCostBadgeComponent } from './session-cost-badge.component';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import type { QuotaStatus } from '../../../services/quota/quota-status.model';

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

describe('SessionCostBadgeComponent quota tooltip', () => {
  let quotaValue: QuotaStatus | undefined;

  function build() {
    const chatStub = {
      costDollars: () => 0.4175,
      costDollarsFor: () => 0.4175,
      contextTokens: () => 1000,
      contextTokensFor: () => 1000,
      contextWindowSize: () => 200000,
      contextWindowFor: () => 200000,
      contextPct: () => 8,
      contextPctFor: () => 8,
    };
    const quotaStub = { status: { value: () => quotaValue } };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ChatStateService, useValue: chatStub },
        { provide: QuotaStatusService, useValue: quotaStub },
      ],
    });
    const fixture = TestBed.createComponent(SessionCostBadgeComponent);
    fixture.detectChanges();
    return fixture.componentInstance as unknown as {
      quotaInfo: () => ReturnType<any> | null;
      quotaUnlimited: () => boolean;
      hasQuotaTooltip: () => boolean;
      quotaPctClass: () => string;
      costAriaLabel: () => string;
    };
  }

  afterEach(() => TestBed.resetTestingModule());

  it('exposes quota breakdown labels for a normal tier', () => {
    quotaValue = quota();
    const c = build();
    const q = c.quotaInfo()!;
    expect(q.pctLabel).toBe('25%');
    expect(q.usageLabel).toBe('$2.50');
    expect(q.limitLabel).toBe('$10.00');
    expect(q.remainingLabel).toBe('$7.50');
    expect(q.periodWord).toBe('month');
    expect(c.hasQuotaTooltip()).toBe(true);
    expect(c.quotaPctClass()).toContain('success');
  });

  it('turns the percentage red at/above 90%', () => {
    quotaValue = quota({ usagePercentage: 95, currentUsage: 9.5, remaining: 0.5 });
    const c = build();
    expect(c.quotaPctClass()).toContain('danger');
  });

  it('reports unlimited with no breakdown', () => {
    quotaValue = quota({ unlimited: true, monthlyLimit: null });
    const c = build();
    expect(c.quotaInfo()).toBeNull();
    expect(c.quotaUnlimited()).toBe(true);
    expect(c.hasQuotaTooltip()).toBe(true);
    expect(c.costAriaLabel()).toContain('Unlimited');
  });

  it('has no tooltip when quota is unconfigured', () => {
    quotaValue = quota({ configured: false, monthlyLimit: null });
    const c = build();
    expect(c.quotaInfo()).toBeNull();
    expect(c.quotaUnlimited()).toBe(false);
    expect(c.hasQuotaTooltip()).toBe(false);
  });

  it('folds monthly quota into the accessible label', () => {
    quotaValue = quota();
    const c = build();
    expect(c.costAriaLabel()).toContain('Monthly quota');
    expect(c.costAriaLabel()).toContain('$10.00');
  });
});
