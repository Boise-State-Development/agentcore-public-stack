import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { UserService } from '../../../auth/user.service';
import { AdminQuotaRequest, QuotaRequestDetail, QuotaRequestStatus } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import { RequestDetailPage } from './request-detail.page';

function detail(status: QuotaRequestStatus, userId = 'u-requester'): QuotaRequestDetail {
  const request: AdminQuotaRequest = {
    requestId: 'req-1',
    userId,
    userEmail: 'requester@example.edu',
    status,
    kind: 'one_time',
    requestedAmount: '25',
    requestedUntil: '2026-11-30',
    justification: 'A grant proposal is due Nov 15.\nThe agent is drafting it.',
    context: { periodType: 'monthly', period: '2026-10', baseLimit: '30', currentUsage: '31', percentageUsed: '103', blocked: true },
    createdAt: '2026-10-30T10:00:00Z',
    updatedAt: '2026-10-30T10:00:00Z',
  };
  return {
    request,
    user: { userId, email: 'requester@example.edu', name: 'Riley Requester', roles: ['Faculty'], matchedBy: 'default_tier' },
    live: { periodType: 'monthly', period: '2026-10', baseLimit: '30', effectiveLimit: '30', currentUsage: '31.5', percentageUsed: '105', blocked: true, tierName: 'Faculty' },
    previousPeriods: [{ period: '2026-09', spend: 12.5 }],
    priorRequests: [],
    extensions: [],
  };
}

describe('RequestDetailPage', () => {
  let service: Record<string, ReturnType<typeof vi.fn>>;
  let me: string;

  beforeEach(() => {
    TestBed.resetTestingModule();
    me = 'u-admin';
    service = {
      getRequest: vi.fn(),
      getPolicy: vi.fn().mockResolvedValue({ fundingFields: [] }),
      deny: vi.fn(),
      requestInfo: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: QuotaRequestsAdminService, useValue: service },
        {
          provide: UserService,
          useValue: { currentUser: signal({ user_id: me }).asReadonly(), hasAdminScope: () => true },
        },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ requestId: 'req-1' }) } } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function render(value: QuotaRequestDetail): Promise<HTMLElement> {
    service['getRequest'].mockResolvedValue(value);
    const fixture = TestBed.createComponent(RequestDetailPage);
    fixture.autoDetectChanges();
    await fixture.whenStable();
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  function buttons(el: HTMLElement): string[] {
    return [...el.querySelectorAll('button')].map(b => (b.textContent ?? '').trim());
  }

  it('shows live usage beside usage when they asked, and the full justification', async () => {
    const el = await render(detail('pending'));
    const text = (el.textContent ?? '').replace(/\s+/g, ' ');
    expect(text).toContain('$31.50 of $30 (105%)');
    expect(text).toContain('$31.00 of $30 (103%)');
    expect(text).toContain('The agent is drafting it.');
    expect(text).toContain('2026-09: $12.50');
  });

  it('offers all three decisions on a pending request', async () => {
    const el = await render(detail('pending'));
    expect(buttons(el)).toEqual(['Approve…', 'Ask for info…', 'Deny…']);
  });

  it('drops Ask for info once the request is waiting on the user', async () => {
    const el = await render(detail('info_requested'));
    expect(buttons(el)).toEqual(['Approve…', 'Deny…']);
  });

  it('offers nothing on a decided request', async () => {
    const el = await render(detail('approved'));
    expect(buttons(el)).toEqual([]);
  });

  it('refuses to let an admin decide their own request', async () => {
    const el = await render(detail('pending', 'u-admin'));
    expect(buttons(el)).toEqual([]);
    expect(el.textContent).toContain('This is your own request');
  });

  it('denies with a required note and reloads', async () => {
    const el = await render(detail('pending'));
    service['deny'].mockResolvedValue({ ...detail('denied').request });
    service['getRequest'].mockResolvedValue(detail('denied'));

    [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Deny'))!.click();
    await new Promise(r => setTimeout(r));
    TestBed.tick();
    const dialog = document.querySelector('.cdk-dialog-container') as HTMLElement;
    const send = [...dialog.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Deny') as HTMLButtonElement;
    expect(send.disabled).toBe(true);

    const note = dialog.querySelector('textarea') as HTMLTextAreaElement;
    note.value = 'Please use the department allocation.';
    note.dispatchEvent(new Event('input'));
    TestBed.tick();
    send.click();
    await new Promise(r => setTimeout(r));
    await new Promise(r => setTimeout(r));
    TestBed.tick();

    expect(service['deny']).toHaveBeenCalledWith('req-1', 'Please use the department allocation.');
    expect(el.textContent).toContain('Denied. The user has been notified');
  });
});
