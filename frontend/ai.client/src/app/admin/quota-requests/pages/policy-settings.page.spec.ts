import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { UserApiService } from '../../../users/services/user-api.service';
import { AdminQuotaRequestPolicy } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import { PolicySettingsPage } from './policy-settings.page';

const POLICY: AdminQuotaRequestPolicy = {
  amountPresets: ['10', '25', '50'],
  allowCustomAmount: true,
  maxRequestAmount: '100',
  defaultThrough: 'end_of_period',
  maxDurationDays: 92,
  justificationMinChars: 20,
  fundingSourceMode: 'optional',
  fundingFields: [],
  defaultFundingMode: 'central',
  standingEnabled: true,
  standingDefaultCap: '100',
  fiscalYearEnd: '12-31',
  reportGroupField: null,
  notifyEmails: [],
};

describe('PolicySettingsPage', () => {
  let service: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = {
      getPolicy: vi.fn().mockResolvedValue(POLICY),
      updatePolicy: vi.fn().mockImplementation(async (p: AdminQuotaRequestPolicy) => ({ ...p, updatedAt: '2026-10-31T10:00:00Z' })),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: QuotaRequestsAdminService, useValue: service },
        { provide: UserApiService, useValue: { searchUsers: vi.fn() } },
      ],
    });
  });

  async function render() {
    const fixture = TestBed.createComponent(PolicySettingsPage);
    fixture.autoDetectChanges();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    return fixture;
  }

  function type(el: HTMLElement, selector: string, value: string): void {
    const input = el.querySelector(selector) as HTMLInputElement;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('adds funding fields, previews them, and saves the schema with the standing settings', async () => {
    const fixture = await render();
    const el = fixture.nativeElement as HTMLElement;

    ([...el.querySelectorAll('button')].find(b => b.textContent?.includes('Add a field')) as HTMLButtonElement).click();
    await fixture.whenStable();
    type(el, 'input[aria-label="Key, field 1"]', 'fund');
    type(el, 'input[aria-label="Label, field 1"]', 'Fund');
    type(el, 'input[aria-label="Pattern, field 1"]', '\\d{6}');
    (el.querySelector('input[aria-label="Required, field 1"]') as HTMLInputElement).click();
    type(el, '#policy-fy', '06-30');
    type(el, '#policy-cap', '200');
    await fixture.whenStable();

    // The preview draws the same inputs the approve form will.
    const preview = [...el.querySelectorAll('label')].map(l => (l.textContent ?? '').trim());
    expect(preview.some(l => l.startsWith('Fund'))).toBe(true);

    (el.querySelector('button[type="submit"]') as HTMLButtonElement).click();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();

    expect(service['updatePolicy']).toHaveBeenCalledWith(
      expect.objectContaining({
        fundingFields: [{ key: 'fund', label: 'Fund', required: true, pattern: '\\d{6}', help: null }],
        fiscalYearEnd: '06-30',
        standingDefaultCap: '200',
        amountPresets: ['10', '25', '50'],
        defaultThrough: 'end_of_period',
      }),
    );
    expect(el.textContent).toContain('Saved.');
  });

  it('refuses a malformed fiscal year end without calling the API', async () => {
    const fixture = await render();
    const el = fixture.nativeElement as HTMLElement;
    type(el, '#policy-fy', 'June 30');
    await fixture.whenStable();
    (el.querySelector('button[type="submit"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    expect(service['updatePolicy']).not.toHaveBeenCalled();
    expect(el.textContent).toContain('The fiscal year end is a month and day');
  });
});
