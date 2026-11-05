import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { provideRouter } from '@angular/router';
import { ChargebackReport, QuotaChargeLine } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import { ChargebackReportPage } from './chargeback-report.page';

const FIELDS = [
  { key: 'fund', label: 'Fund', required: true },
  { key: 'org', label: 'Org', required: false },
];

function line(id: string, overrides: Partial<QuotaChargeLine> = {}): QuotaChargeLine {
  return {
    period: '2026-10',
    userId: `user-${id}`,
    userEmail: `${id}@example.edu`,
    extensionId: `ext-${id}-0000000000`,
    fundingMode: 'central',
    fundingKey: null,
    fundingLabel: '',
    fields: {},
    standing: false,
    granted: '10',
    consumed: '10.000000',
    baseLimit: '30',
    baseLimitAmbiguous: false,
    periodSpend: '45',
    approvedBy: 'approver@example.edu',
    approvedAt: '2026-10-01T00:00:00Z',
    status: 'closed',
    ...overrides,
  };
}

const FUND_1 = 'fund=100000|org=4210';
const FUND_2 = 'fund=200000|org=4300';

function report(overrides: Partial<ChargebackReport> = {}): ChargebackReport {
  return {
    periods: [{ period: '2026-10', status: 'closed', closedAt: '2026-11-03T12:00:00Z', closeVersion: 1, closable: true, unattributed: '15' }],
    lines: [
      line('a'),
      line('b', {
        fundingMode: 'chargeback', fundingKey: FUND_1, fields: { fund: '100000', org: '4210' }, groupValue: '4210',
        authorizerEmail: 'owner@example.edu', granted: '20', consumed: '20', requestId: 'req-1',
      }),
      line('c', {
        fundingMode: 'chargeback', fundingKey: FUND_2, fields: { fund: '200000', org: '4300' }, groupValue: '4300',
        standing: true, granted: null, consumed: '15', baseLimitAmbiguous: true, baseLimit: '40',
      }),
    ],
    totals: { central: '10.00', chargeback: '35.00', consumed: '45.00', unattributed: '15.00' },
    fundingFields: FIELDS,
    groupField: 'org',
    consumptionOrder: 'Spend above the base limit is attributed to extensions in this order: centrally funded, then capped chargeback, then standing.',
    ...overrides,
  };
}

describe('ChargebackReportPage', () => {
  let service: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-11-05T12:00:00Z') });
    TestBed.resetTestingModule();
    service = {
      getChargeback: vi.fn().mockResolvedValue(report()),
      getChargebackCsv: vi.fn().mockResolvedValue(new Blob(['period\r\n'], { type: 'text/csv' })),
      listFunds: vi.fn().mockResolvedValue([
        { fundingKey: FUND_1, label: '', fields: { fund: '100000', org: '4210' }, firstPeriod: '2026-10', lastPeriod: '2026-11', extensions: 1, consumedClosed: '20.00' },
      ]),
      getPolicy: vi.fn().mockResolvedValue({ fiscalYearEnd: '06-30', fundingFields: FIELDS }),
      reclosePeriod: vi.fn().mockResolvedValue({
        period: { period: '2026-10', status: 'closed', closeVersion: 2, closable: true, unattributed: '15' },
        before: { [FUND_1]: '20.00' }, after: { [FUND_1]: '10.00' }, changedFunds: [FUND_1],
      }),
    };
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: QuotaRequestsAdminService, useValue: service }],
    });
  });

  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    vi.useRealTimers();
  });

  async function render() {
    const fixture = TestBed.createComponent(ChargebackReportPage);
    fixture.autoDetectChanges();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    return fixture;
  }

  const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
  const rows = (el: HTMLElement) => [...el.querySelectorAll('tbody tr')].map(text);

  it('defaults to the last complete month and groups lines by fund with subtotals', async () => {
    const fixture = await render();
    expect(service['getChargeback']).toHaveBeenCalledWith({ from: '2026-10', to: '2026-10', fundingKey: null, mode: null });
    const lines = rows(fixture.nativeElement);
    expect(lines[0]).toContain('Central budget');
    expect(lines.some(r => r.startsWith('▾ Fund 100000 · Org 4210') && r.includes('Authorized by owner@example.edu') && r.includes('$20.00'))).toBe(true);
    expect(lines.some(r => r.includes('Fund 200000 · Org 4300') && r.includes('Uncapped'))).toBe(true);
    const months = text(fixture.nativeElement.querySelector('ul[aria-label="Months in this report"]'));
    expect(months).toContain('October 2026');
    expect(months).toContain('Closed');
    expect(months).toContain('on Nov 3, 2026');
    expect(text(fixture.nativeElement)).toContain('centrally funded, then capped chargeback, then standing');
  });

  it('marks a line whose base limit changed during the period, and its group while collapsed', async () => {
    const fixture = await render();
    const marked = rows(fixture.nativeElement).filter(r => r.includes('Base limit changed during period'));
    expect(marked).toHaveLength(1);
    expect(marked[0]).toContain('the latest ($40.00) was used');
    expect(marked[0]).toContain('Standing');
    const flaggedGroups = rows(fixture.nativeElement).filter(r => r.includes('A base limit changed during the period'));
    expect(flaggedGroups).toHaveLength(1);
    expect(flaggedGroups[0]).toContain('200000');
  });

  it('labels an open month provisional with the date it closes', async () => {
    service['getChargeback'].mockResolvedValue(report({
      periods: [{ period: '2026-11', status: 'provisional', closable: false, unattributed: '0' }],
    }));
    const fixture = await render();
    const page = text(fixture.nativeElement);
    expect(page).toContain('Provisional');
    expect(page).toContain('closes Dec 3, 2026');
    expect(fixture.nativeElement.querySelector('button[aria-label^="Recalculate"]')).toBeNull();
  });

  it('collapses to one row per group under the grouping field, with a drill-down', async () => {
    const fixture = await render();
    const select = fixture.nativeElement.querySelector('#cb-group') as HTMLSelectElement;
    expect([...select.options].map(o => o.text)).toEqual(['Funding source', 'Org']);
    select.value = 'group';
    select.dispatchEvent(new Event('change'));
    await fixture.whenStable();
    let lines = rows(fixture.nativeElement);
    expect(lines).toHaveLength(3);
    expect(lines.some(r => r.startsWith('▸ Org 4210'))).toBe(true);
    const toggle = [...fixture.nativeElement.querySelectorAll('button[aria-expanded]')]
      .find(b => text(b).includes('Org 4210')) as HTMLButtonElement;
    toggle.click();
    await fixture.whenStable();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    lines = rows(fixture.nativeElement);
    expect(lines.some(r => r.includes('b@example.edu'))).toBe(true);
  });

  it('passes the fund and mode filters and exports the same query as CSV', async () => {
    const fixture = await render();
    const fund = fixture.nativeElement.querySelector('#cb-fund') as HTMLSelectElement;
    fund.value = FUND_1;
    fund.dispatchEvent(new Event('change'));
    const mode = fixture.nativeElement.querySelector('#cb-mode') as HTMLSelectElement;
    mode.value = 'chargeback';
    mode.dispatchEvent(new Event('change'));
    await fixture.whenStable();
    expect(service['getChargeback']).toHaveBeenLastCalledWith({ from: '2026-10', to: '2026-10', fundingKey: FUND_1, mode: 'chargeback' });

    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    (URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn().mockReturnValue('blob:x');
    (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
    const exportButton = [...fixture.nativeElement.querySelectorAll('button')].find(b => text(b) === 'Export CSV') as HTMLButtonElement;
    exportButton.click();
    await fixture.whenStable();
    expect(service['getChargebackCsv']).toHaveBeenCalledWith({ from: '2026-10', to: '2026-10', fundingKey: FUND_1, mode: 'chargeback' });
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it('recalculates a closed month after confirmation and names the fund that changed', async () => {
    const fixture = await render();
    const button = fixture.nativeElement.querySelector('button[aria-label="Recalculate October 2026"]') as HTMLButtonElement;
    button.click();
    await fixture.whenStable();
    const confirm = [...document.querySelectorAll('button')].find(b => text(b) === 'Recalculate' && !b.getAttribute('aria-label')) as HTMLButtonElement;
    expect(confirm).toBeTruthy();
    confirm.click();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    expect(service['reclosePeriod']).toHaveBeenCalledWith('2026-10');
    expect(text(fixture.nativeElement.querySelector('[role="status"]'))).toBe(
      'Recalculated October 2026. Fund 100000 · Org 4210 $20.00 → $10.00.',
    );
  });
});
