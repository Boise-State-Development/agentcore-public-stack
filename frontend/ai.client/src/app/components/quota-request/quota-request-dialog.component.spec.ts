import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { QuotaRequestDialogComponent, QuotaRequestDialogData } from './quota-request-dialog.component';
import { QuotaRequestFailure, QuotaRequestService } from '../../services/quota/quota-request.service';
import { QuotaStatusService } from '../../services/quota/quota-status.service';
import type { QuotaRequestPolicy } from '../../services/quota/quota-request.model';
import type { QuotaStatus } from '../../services/quota/quota-status.model';

/** The policy as the API sends it: amounts as decimal strings. */
function policy(over: Partial<QuotaRequestPolicy> = {}): QuotaRequestPolicy {
  return {
    amountPresets: ['10', '25', '50'],
    allowCustomAmount: true,
    maxRequestAmount: '100',
    defaultThrough: 'end_of_period',
    maxDurationDays: 92,
    justificationMinChars: 20,
    fundingSourceMode: 'hidden',
    fundingFields: [],
    standingEnabled: false,
    fiscalYearEnd: '06-30',
    ...over,
  };
}

const STATUS: QuotaStatus = {
  configured: true,
  unlimited: false,
  tierName: 'Standard',
  matchedBy: 'jwt_role:Faculty',
  monthlyLimit: 30,
  currentUsage: 28.4,
  remaining: 1.6,
  usagePercentage: 94.7,
  periodType: 'monthly',
  resetInfo: 'Quota resets in 12 day(s)',
  hasActiveOverride: false,
  baseLimit: 30,
  effectiveLimit: 30,
  extensions: [],
};

type Api = {
  amountChoice: { set(v: string): void; (): string };
  customAmount: { set(v: string): void };
  through: { set(v: string): void; (): string };
  justification: { set(v: string): void };
  fundingValues: { set(v: Record<string, string>): void };
  fundingLabel: { set(v: string): void };
  standing: () => boolean;
  setStanding(on: boolean): void;
  canSend(): boolean;
  amountError(): string | null;
  throughHint(): string | null;
  headerLine(): string | null;
  send(): Promise<void>;
  sent(): string | undefined;
  error(): string | null;
};

describe('QuotaRequestDialogComponent', () => {
  let service: {
    policy: ReturnType<typeof vi.fn>;
    submit: ReturnType<typeof vi.fn>;
    addFundingSource: ReturnType<typeof vi.fn>;
    withdraw: ReturnType<typeof vi.fn>;
  };
  let close: ReturnType<typeof vi.fn>;

  function configure(p: QuotaRequestPolicy, data: QuotaRequestDialogData = {}) {
    TestBed.resetTestingModule();
    service = {
      policy: vi.fn().mockResolvedValue(p),
      submit: vi.fn().mockResolvedValue({}),
      addFundingSource: vi.fn().mockResolvedValue({}),
      withdraw: vi.fn().mockResolvedValue({}),
    };
    close = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        { provide: QuotaRequestService, useValue: service },
        { provide: QuotaStatusService, useValue: { status: { value: signal(STATUS) } } },
        { provide: DIALOG_DATA, useValue: data },
        { provide: DialogRef, useValue: { close } },
      ],
    });
  }

  async function create(p: QuotaRequestPolicy = policy(), data: QuotaRequestDialogData = {}) {
    configure(p, data);
    const fixture = TestBed.createComponent(QuotaRequestDialogComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, api: fixture.componentInstance as unknown as Api, el: fixture.nativeElement as HTMLElement };
  }

  beforeEach(() => TestBed.resetTestingModule());
  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  it('is a named dialog through a real CDK Dialog', async () => {
    configure(policy());
    const { container } = await openInCdkDialog(QuotaRequestDialogComponent, { data: {} });
    expectNamedDialog(container, { name: 'Request more quota' });
  });

  it('heads with the live quota status and offers the policy presets, parsed from strings', async () => {
    const { api, el } = await create();
    expect(api.headerLine()).toBe('You’ve used $28.40 of your $30.00 monthly quota · Quota resets in 12 day(s)');
    const labels = [...el.querySelectorAll('input[name="quota-request-amount"]')].map(i => i.parentElement!.textContent!.trim());
    expect(labels).toEqual(['$10', '$25', '$50', 'Other']);
    // The middle preset is pre-selected.
    expect(api.amountChoice()).toBe('25');
  });

  it('defaults through the end of the period', async () => {
    const { api } = await create();
    const now = new Date();
    const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).toISOString().slice(0, 10);
    expect(api.through()).toBe(end);
    expect(api.throughHint()).toBeNull();
  });

  it('refuses a custom amount above the cap inline, naming it', async () => {
    const { api } = await create();
    api.amountChoice.set('other');
    api.customAmount.set('150');
    api.justification.set('A long enough reason for this request.');
    expect(api.amountError()).toBe('Requests are limited to $100 per month; your administrator can grant more.');
    expect(api.canSend()).toBe(false);
  });

  it('hides Other when the policy allows presets only', async () => {
    const { el } = await create(policy({ allowCustomAmount: false }));
    expect(el.textContent).not.toContain('Other');
  });

  it('waits on a justification of the minimum length', async () => {
    const { api } = await create();
    api.justification.set('too short');
    expect(api.canSend()).toBe(false);
    api.justification.set('Finishing the grant report this week.');
    expect(api.canSend()).toBe(true);
  });

  it('warns when the end date falls mid-month', async () => {
    const { api } = await create();
    const now = new Date();
    const mid = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 15)).toISOString().slice(0, 10);
    api.through.set(mid);
    expect(api.throughHint()).toBe('Your quota drops back to $30 on that date, mid-month.');
  });

  it('sends the request and says what happens next', async () => {
    const { api, fixture, el } = await create();
    api.justification.set('Finishing the grant report this week.');
    await api.send();
    fixture.detectChanges();
    expect(service.submit).toHaveBeenCalledWith({
      kind: 'one_time',
      requestedAmount: 25,
      requestedUntil: api.through(),
      justification: 'Finishing the grant report this week.',
      fundingSource: null,
    });
    expect(el.textContent).toContain('Sent. You’ll see the decision here and in your notifications.');
  });

  it('shows a refusal from the backend verbatim', async () => {
    const { api } = await create();
    service.submit.mockRejectedValue(new QuotaRequestFailure('You already have an open quota request', 409, 'r1'));
    api.justification.set('Finishing the grant report this week.');
    await api.send();
    expect(api.error()).toBe('You already have an open quota request');
    expect(api.sent()).toBeUndefined();
  });

  it('requires a funding source for an ongoing request and sends it', async () => {
    const { api } = await create(
      policy({
        fundingSourceMode: 'optional',
        standingEnabled: true,
        fundingFields: [
          { key: 'fund', label: 'Fund', required: true, pattern: '\\d{6}' },
          { key: 'org', label: 'Org', required: false },
        ],
      }),
    );
    api.justification.set('Ongoing research spend on grant 12.');
    expect(api.canSend()).toBe(true);
    api.setStanding(true);
    expect(api.canSend()).toBe(false);
    api.fundingValues.set({ fund: '12' });
    expect(api.canSend()).toBe(false);
    api.fundingValues.set({ fund: '100000' });
    expect(api.canSend()).toBe(true);
    await api.send();
    expect(service.submit).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'standing', fundingSource: { fields: { fund: '100000' } } }),
    );
  });

  it('answers a request for a funding source with a PATCH, showing the admin’s note', async () => {
    const { api, el } = await create(
      policy({ fundingFields: [{ key: 'fund', label: 'Fund', required: true }] }),
      { fundingFor: { requestId: 'r1', adminNote: 'Which fund should this go to?' } },
    );
    expect(el.textContent).toContain('Which fund should this go to?');
    expect(el.querySelector('#quota-request-why')).toBeNull();
    expect(api.canSend()).toBe(false);
    api.fundingValues.set({ fund: '100000' });
    await api.send();
    expect(service.addFundingSource).toHaveBeenCalledWith('r1', { fields: { fund: '100000' } });
    expect(api.sent()).toBe('funding_added');
  });

  it('answers with the fund’s name alone where the deployment defines no fields', async () => {
    const { api, el } = await create(policy(), { fundingFor: { requestId: 'r1', adminNote: 'Which fund?' } });
    expect(el.querySelector('#quota-funding-label')).not.toBeNull();
    expect(api.canSend()).toBe(false);
    api.fundingLabel.set('COAS Dean’s Office');
    await api.send();
    expect(service.addFundingSource).toHaveBeenCalledWith('r1', { fields: {}, label: 'COAS Dean’s Office' });
  });
});
