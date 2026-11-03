import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { provideRouter } from '@angular/router';
import { QuotaExtension } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import { ExtensionListPage } from './extension-list.page';

function extension(id: string, overrides: Partial<QuotaExtension> = {}): QuotaExtension {
  return {
    extensionId: id,
    userId: `user-${id}`,
    userEmail: `${id}@example.edu`,
    standing: false,
    amount: '20',
    validFrom: '2026-10-01T00:00:00Z',
    validUntil: '2099-11-30T23:59:59Z',
    fundingMode: 'central',
    reason: 'pilot',
    createdBy: 'admin',
    createdByEmail: 'approver@example.edu',
    createdAt: '2026-10-30T10:00:00Z',
    enabled: true,
    ...overrides,
  };
}

const ROWS = [
  extension('one'),
  extension('standing', {
    standing: true,
    amount: '200',
    fundingMode: 'chargeback',
    fundingSource: { fields: { fund: '100000' } },
  }),
];

describe('ExtensionListPage', () => {
  let service: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = {
      listExtensions: vi.fn().mockResolvedValue(ROWS),
      getPolicy: vi.fn().mockResolvedValue({ fundingFields: [{ key: 'fund', label: 'Fund', required: true }] }),
      revokeExtension: vi.fn().mockResolvedValue({ ...ROWS[0], revokedAt: '2026-10-31T00:00:00Z' }),
    };
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: QuotaRequestsAdminService, useValue: service }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function render() {
    const fixture = TestBed.createComponent(ExtensionListPage);
    fixture.autoDetectChanges();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    return fixture;
  }

  function rows(el: HTMLElement): string[] {
    return [...el.querySelectorAll('tbody tr')].map(r => (r.textContent ?? '').replace(/\s+/g, ' ').trim());
  }

  it('lists active extensions with the fund by its schema label', async () => {
    const fixture = await render();
    expect(service['listExtensions']).toHaveBeenCalledWith({ active: true });
    const lines = rows(fixture.nativeElement);
    expect(lines[0]).toContain('+$20 per month');
    expect(lines[1]).toContain('Cap $200 per month');
    expect(lines[1]).toContain('Fund 100000');
  });

  it('narrows to standing arrangements with the Standing filter', async () => {
    const fixture = await render();
    const select = fixture.nativeElement.querySelector('#extension-kind') as HTMLSelectElement;
    select.value = 'standing';
    select.dispatchEvent(new Event('change'));
    await fixture.whenStable();
    const lines = rows(fixture.nativeElement);
    expect(lines.length).toBe(1);
    expect(lines[0]).toContain('standing@example.edu');
  });

  it('revokes with a reason and reloads', async () => {
    const fixture = await render();
    (fixture.nativeElement.querySelector('button[aria-label="Revoke the extension for one@example.edu"]') as HTMLButtonElement).click();
    await new Promise(r => setTimeout(r));
    TestBed.tick();
    const dialog = document.querySelector('.cdk-dialog-container') as HTMLElement;
    const reason = dialog.querySelector('textarea') as HTMLTextAreaElement;
    reason.value = 'Pilot ended';
    reason.dispatchEvent(new Event('input'));
    TestBed.tick();
    ([...dialog.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Revoke') as HTMLButtonElement).click();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();

    expect(service['revokeExtension']).toHaveBeenCalledWith('one', 'Pilot ended');
    expect(service['listExtensions']).toHaveBeenCalledTimes(2);
    expect(fixture.nativeElement.textContent).toContain('Revoked. one@example.edu has been notified.');
  });
});
