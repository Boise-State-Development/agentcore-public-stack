import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { provideRouter } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { UserApiService } from '../../../users/services/user-api.service';
import { AdminQuotaRequest, AdminQuotaRequestPolicy } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import {
  ExtendQuotaDialogComponent,
  ExtendQuotaDialogData,
  ExtendQuotaDialogResult,
} from './extend-quota-dialog.component';

const POLICY: AdminQuotaRequestPolicy = {
  amountPresets: ['10', '25', '50'],
  allowCustomAmount: true,
  maxRequestAmount: '100',
  defaultThrough: 'end_of_period',
  maxDurationDays: 92,
  justificationMinChars: 20,
  fundingSourceMode: 'optional',
  fundingFields: [
    { key: 'fund', label: 'Fund', required: true, pattern: '\\d{6}' },
    { key: 'org', label: 'Org', required: false },
  ],
  defaultFundingMode: 'central',
  standingEnabled: true,
  standingDefaultCap: '200',
  fiscalYearEnd: '06-30',
  reportGroupField: null,
  notifyEmails: [],
};

function request(overrides: Partial<AdminQuotaRequest> = {}): AdminQuotaRequest {
  return {
    requestId: 'req-1',
    userId: 'u-1',
    userEmail: 'requester@example.edu',
    status: 'pending',
    kind: 'one_time',
    requestedAmount: '25',
    requestedUntil: '2099-11-30',
    justification: 'A grant proposal is due and the agent is drafting it.',
    context: { periodType: 'monthly', period: '2099-10', currentUsage: '31', percentageUsed: '103', blocked: true },
    createdAt: '2099-10-30T10:00:00Z',
    updatedAt: '2099-10-30T10:00:00Z',
    ...overrides,
  };
}

function text(container: HTMLElement): string {
  return (container.textContent ?? '').replace(/\s+/g, ' ');
}

function button(container: HTMLElement, label: RegExp): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find(b => label.test(b.textContent ?? ''));
  if (!found) throw new Error(`no button ${label}`);
  return found as HTMLButtonElement;
}

function type(input: Element | null, value: string): void {
  const el = input as HTMLInputElement;
  el.value = value;
  el.dispatchEvent(new Event('input'));
}

async function settle(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0));
  TestBed.tick();
}

describe('ExtendQuotaDialogComponent', () => {
  let service: { approve: ReturnType<typeof vi.fn>; createExtension: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = {
      approve: vi.fn().mockResolvedValue({ request: request({ status: 'approved' }), extension: { extensionId: 'ext-1' } }),
      createExtension: vi.fn().mockResolvedValue({ extensionId: 'ext-2', userEmail: 'requester@example.edu' }),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: QuotaRequestsAdminService, useValue: service },
        { provide: UserApiService, useValue: { searchUsers: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(data: ExtendQuotaDialogData) {
    const opened = await openInCdkDialog<ExtendQuotaDialogComponent, ExtendQuotaDialogData, ExtendQuotaDialogResult>(
      ExtendQuotaDialogComponent,
      { data },
    );
    TestBed.tick();
    return opened;
  }

  it('is a named dialog prefilled from the request', async () => {
    const { container } = await open({ mode: 'approve', request: request(), policy: POLICY, userLabel: 'Riley' });
    expectNamedDialog(container, { name: 'Approve request' });
    expect((container.querySelector('#extend-amount') as HTMLInputElement).value).toBe('25');
    expect((container.querySelector('#extend-through') as HTMLInputElement).value).toBe('2099-11-30');
    expect(text(container)).toContain('They asked for $25 per month');
  });

  it('approves a one-time request centrally with the edited amount', async () => {
    const { container, ref } = await open({ mode: 'approve', request: request(), policy: POLICY, userLabel: 'Riley' });
    type(container.querySelector('#extend-amount'), '20');
    TestBed.tick();
    const closed = firstValueFrom(ref.closed);
    button(container, /^\s*Approve\s*$/).click();
    const result = await closed;
    expect(service.approve).toHaveBeenCalledWith('req-1', {
      amount: 20,
      validUntil: '2099-11-30',
      fundingMode: 'central',
      fundingSource: null,
      standing: false,
      note: null,
    });
    expect(result?.kind).toBe('approved');
  });

  it('offers "Ask the user for a funding source" when charging back with no fund', async () => {
    const { container, ref } = await open({ mode: 'approve', request: request(), policy: POLICY, userLabel: 'Riley' });
    const chargeback = [...container.querySelectorAll<HTMLInputElement>('input[name="extend-funding"]')][1];
    chargeback.click();
    TestBed.tick();
    const closed = firstValueFrom(ref.closed);
    button(container, /Ask the user for a funding source/).click();
    expect(await closed).toEqual({ kind: 'askForFunding' });
    expect(service.approve).not.toHaveBeenCalled();
  });

  it('warns on No cap and asks for a confirmation naming the user, the fund and the cap', async () => {
    const { container, ref } = await open({
      mode: 'direct',
      policy: POLICY,
      user: { userId: 'u-1', email: 'requester@example.edu', name: 'Riley Requester' },
    });
    expectNamedDialog(container, { name: 'Extend quota' });

    container.querySelector<HTMLInputElement>('input[value="standing"]')!.click();
    TestBed.tick();
    expect((container.querySelector('#extend-amount') as HTMLInputElement).value).toBe('200');
    expect(button(container, /Create standing chargeback/)).toBeTruthy();

    const noCap = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')][0];
    noCap.click();
    TestBed.tick();
    expect(text(container)).toContain('Without a cap one runaway conversation can bill this fund without limit.');
    noCap.click();
    TestBed.tick();

    const fund = container.querySelector('input[id$="-fund"]');
    type(fund, '100000');
    type(container.querySelector('#extend-reason'), 'Grant-funded research run');
    TestBed.tick();

    button(container, /Create standing chargeback/).click();
    await settle();
    const sentence = container.querySelector('[data-testid="standing-confirmation"]')?.textContent ?? '';
    expect(sentence).toContain("Riley Requester's usage will be charged to Fund 100000, up to $200 per month");
    expect(service.createExtension).not.toHaveBeenCalled();

    const closed = firstValueFrom(ref.closed);
    button(container, /^\s*Confirm\s*$/).click();
    await closed;
    expect(service.createExtension).toHaveBeenCalledWith(
      expect.objectContaining({
        userId: 'u-1',
        standing: true,
        amount: 200,
        fundingMode: 'chargeback',
        fundingSource: { label: null, fields: { fund: '100000' }, authorizerEmail: null },
        reason: 'Grant-funded research run',
      }),
    );
  });

  it('turns the standing switch off with a link to Settings when no funding fields exist', async () => {
    const { container } = await open({ mode: 'direct', policy: { ...POLICY, fundingFields: [] } });
    expect(container.querySelector<HTMLInputElement>('input[value="standing"]')!.disabled).toBe(true);
    expect(container.querySelector('a[href="/admin/quota-requests/settings"]')).not.toBeNull();
  });

  it('needs a picked user and a reason for a direct extension', async () => {
    const { container } = await open({ mode: 'direct', policy: POLICY });
    expect(container.querySelector('[role="combobox"]')).not.toBeNull();
    type(container.querySelector('#extend-amount'), '10');
    TestBed.tick();
    button(container, /^\s*Extend quota\s*$/).click();
    await settle();
    expect(service.createExtension).not.toHaveBeenCalled();
    expect(button(container, /^\s*Extend quota\s*$/).disabled).toBe(true);
  });
});
