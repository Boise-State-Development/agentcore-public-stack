import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { QuotaRequestActionComponent } from './quota-request-action.component';
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

  function render(quotaRequests: boolean, variant: 'inline' | 'button' = 'inline') {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: FEATURES, useValue: { ...environment.features, quotaRequests } },
        { provide: QuotaStatusService, useValue: { status: { value } } },
        { provide: QuotaRequestService, useValue: { withdraw } },
        { provide: QuotaRequestDialogService, useValue: { open } },
      ],
    });
    const fixture = TestBed.createComponent(QuotaRequestActionComponent);
    fixture.componentRef.setInput('variant', variant);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
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
});
