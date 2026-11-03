import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { DECISION_POLL_MS, QuotaRequestActionComponent } from './quota-request-action.component';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { QuotaRequestDialogService } from './quota-request-dialog.service';
import { QuotaRequestService } from '../../services/quota/quota-request.service';
import { QuotaStatusService } from '../../services/quota/quota-status.service';
import { FEATURES } from '../../services/features';
import { environment } from '../../../environments/environment';
import type { QuotaStatus } from '../../services/quota/quota-status.model';

function status(over: Partial<QuotaStatus> = {}): QuotaStatus {
  return {
    configured: true, unlimited: false, tierName: 'Standard', matchedBy: null, monthlyLimit: 30,
    currentUsage: 25, remaining: 5, usagePercentage: 83, periodType: 'monthly', resetInfo: null,
    hasActiveOverride: false, openRequest: null, ...over,
  };
}

describe('QuotaRequestActionComponent', () => {
  const value = signal<QuotaStatus | undefined>(undefined);
  const open = vi.fn().mockResolvedValue(undefined);
  const withdraw = vi.fn().mockResolvedValue({});
  const refreshInbox = vi.fn().mockResolvedValue(undefined);

  function render(quotaRequests: boolean, variant: 'inline' | 'button' = 'inline') {
    return mount(quotaRequests, variant).nativeElement as HTMLElement;
  }

  function mount(quotaRequests: boolean, variant: 'inline' | 'button' = 'inline', blocked = false) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, quotaRequests } },
        { provide: QuotaStatusService, useValue: { status: { value } } },
        { provide: QuotaRequestService, useValue: { withdraw } },
        { provide: QuotaRequestDialogService, useValue: { open } },
        { provide: NotificationsService, useValue: { refresh: refreshInbox } },
      ],
    });
    const fixture = TestBed.createComponent(QuotaRequestActionComponent);
    fixture.componentRef.setInput('variant', variant);
    fixture.componentRef.setInput('blocked', blocked);
    fixture.detectChanges();
    return fixture;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    value.set(status());
  });
  afterEach(() => TestBed.resetTestingModule());

  it('renders nothing while the feature is off', () => {
    expect(render(false).textContent?.trim()).toBe('');
  });

  it('renders nothing for an unlimited quota, or before the status loads', () => {
    value.set(status({ unlimited: true }));
    expect(render(true).textContent?.trim()).toBe('');
    value.set(undefined);
    expect(render(true).textContent?.trim()).toBe('');
  });

  it('offers a request and opens the dialog', () => {
    const el = render(true, 'button');
    const button = el.querySelector('button')!;
    expect(button.textContent?.trim()).toBe('Request more quota');
    button.click();
    expect(open).toHaveBeenCalledWith();
  });

  it('shows a pending request with Withdraw instead of the button', () => {
    value.set(status({ openRequest: { requestId: 'r1', status: 'pending', requestedAmount: 25, createdAt: '2026-10-30T15:00:00Z' } }));
    const el = render(true);
    expect(el.textContent?.replace(/\s+/g, ' ').trim()).toBe('Request pending · sent Oct 30 · Withdraw');
    el.querySelector('button')!.click();
    expect(withdraw).toHaveBeenCalledWith('r1');
  });

  it('asks for the funding source the admin requested and reopens the dialog on it', () => {
    value.set(status({ openRequest: { requestId: 'r1', status: 'info_requested', requestedAmount: 25, createdAt: '2026-10-30T15:00:00Z', adminNote: 'Which fund?' } }));
    const el = render(true);
    expect(el.textContent?.replace(/\s+/g, ' ').trim()).toBe('Your admin asked for a funding source · Add it');
    el.querySelector('button')!.click();
    expect(open).toHaveBeenCalledWith({ fundingFor: { requestId: 'r1', adminNote: 'Which fund?' } });
  });

  it('says the fund cap ran out once a reloaded status shows the charge at its cap', () => {
    // The status as read at page load: nothing charged yet.
    const fixture = mount(true, 'inline', true);
    const tab = fixture.nativeElement as HTMLElement;
    const text = () => tab.textContent?.replace(/\s+/g, ' ').replace(/·(?=\S)/g, '· ').trim();
    expect(text()).toBe('Quota reached · Request more');

    value.set(status({ charged: { label: 'Fund 100000', thisPeriod: 0.02, cap: 0.02 } }));
    fixture.detectChanges();
    expect(text()).toBe('Fund cap reached · Request a higher cap');
  });

  describe('looking for a decision', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    const pending = () =>
      status({ openRequest: { requestId: 'r1', status: 'pending', requestedAmount: 25, createdAt: '2026-10-30T15:00:00Z' } });

    it('re-reads the inbox while a request is open, so a decision arrives without opening the bell', () => {
      value.set(pending());
      render(true);
      vi.advanceTimersByTime(DECISION_POLL_MS);
      expect(refreshInbox).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(DECISION_POLL_MS);
      expect(refreshInbox).toHaveBeenCalledTimes(2);
    });

    it('stops once the request is decided, and never polls without one', () => {
      value.set(pending());
      const fixture = mount(true);
      value.set(status());
      fixture.detectChanges();
      vi.advanceTimersByTime(DECISION_POLL_MS * 3);
      expect(refreshInbox).not.toHaveBeenCalled();
    });

    it('skips a hidden page', () => {
      value.set(pending());
      render(true);
      const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
      vi.advanceTimersByTime(DECISION_POLL_MS * 2);
      expect(refreshInbox).not.toHaveBeenCalled();
      visibility.mockRestore();
    });
  });
});
