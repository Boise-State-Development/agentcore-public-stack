import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { QuotaWarningBannerComponent } from './quota-warning-banner.component';
import { QuotaWarningService } from '../../services/quota/quota-warning.service';
import { SessionService } from '../../session/services/session/session.service';
import { QuotaStatusService } from '../../services/quota/quota-status.service';
import { QuotaRequestService } from '../../services/quota/quota-request.service';
import { QuotaRequestDialogService } from '../quota-request/quota-request-dialog.service';
import { FEATURES } from '../../services/features';
import { environment } from '../../../environments/environment';

describe('QuotaWarningBannerComponent', () => {
  let warnings: QuotaWarningService;

  function render(_quotaRequests: boolean) {
    const fixture = TestBed.createComponent(QuotaWarningBannerComponent);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  function configure(quotaRequests: boolean) {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, quotaRequests } },
        { provide: SessionService, useValue: { currentSession: signal({ sessionId: 's1' }) } },
        {
          provide: QuotaStatusService,
          useValue: { status: { value: signal({ configured: true, unlimited: false, openRequest: null }) } },
        },
        { provide: QuotaRequestService, useValue: {} },
        { provide: QuotaRequestDialogService, useValue: { open: () => Promise.resolve(undefined) } },
      ],
    });
    warnings = TestBed.inject(QuotaWarningService);
  }

  const warning = (percentageUsed: number) => ({
    type: 'quota_warning' as const,
    warningLevel: `${percentageUsed}%`,
    currentUsage: percentageUsed * 0.3,
    quotaLimit: 30,
    percentageUsed,
    remaining: 30 - percentageUsed * 0.3,
    message: '',
  });

  const exceeded = {
    type: 'quota_exceeded' as const,
    currentUsage: 31, quotaLimit: 30, percentageUsed: 103, periodType: 'monthly', resetInfo: '', message: '',
  };

  beforeEach(() => configure(true));
  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  it('offers a request from the soft limit up, not on the 50% and 75% rungs', () => {
    warnings.setWarning(warning(75));
    const { fixture, el } = render(true);
    expect(el.textContent).not.toContain('Request more');
    warnings.setWarning(warning(80));
    fixture.detectChanges();
    expect(el.textContent).toContain('Request more');
  });

  it('gives a blocked user a tab with Request more quota', () => {
    warnings.setQuotaExceeded(exceeded);
    const { el } = render(true);
    const tab = el.querySelector('[data-testid="quota-blocked-tab"]')!;
    expect(tab.textContent).toContain('Quota reached');
    expect(tab.querySelector('button')!.textContent?.trim()).toBe('Request more quota');
  });

  it('offers nothing new while the feature is off', () => {
    configure(false);
    warnings.setWarning(warning(90));
    warnings.setQuotaExceeded(exceeded);
    warnings.setWarning(warning(90));
    const { el } = render(false);
    expect(el.textContent).not.toContain('Request more');
    expect(el.querySelector('[data-testid="quota-blocked-tab"]')).toBeNull();
  });

  it('shows the funding notice, whatever the flag, until dismissed', () => {
    configure(false);
    warnings.setFundingNotice({
      type: 'quota_funding_notice', label: 'Fund 100000', chargedThisPeriod: 3.2, cap: 200,
      message: 'Included quota used · charging Fund 100000 · $3.20 this month of $200.00',
    });
    const { fixture, el } = render(false);
    const notice = el.querySelector('[data-testid="quota-funding-notice"]')!;
    expect(notice.textContent).toContain('charging Fund 100000');
    (notice.querySelector('button') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="quota-funding-notice"]')).toBeNull();
  });
});
