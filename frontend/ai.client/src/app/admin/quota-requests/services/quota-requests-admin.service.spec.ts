import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ConfigService } from '../../../services/config.service';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from './quota-requests-admin.service';

describe('QuotaRequestsAdminService', () => {
  let service: QuotaRequestsAdminService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://api.test') } },
      ],
    });
    service = TestBed.inject(QuotaRequestsAdminService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('badges the pending count and keeps it through a failed refresh', async () => {
    const first = service.refreshSummary();
    http.expectOne('http://api.test/admin/quota-requests/summary').flush({ pending: 3, infoRequested: 1 });
    await first;
    expect(service.pendingCount()).toBe(3);

    const second = service.refreshSummary();
    http.expectOne('http://api.test/admin/quota-requests/summary').flush('no', { status: 500, statusText: 'Error' });
    await second;
    expect(service.pendingCount()).toBe(3);
  });

  it('approves with the form body and then re-reads the badge', async () => {
    const approving = service.approve('req-1', {
      amount: 20,
      validUntil: '2026-11-30',
      fundingMode: 'central',
      fundingSource: null,
      standing: false,
      note: null,
    });
    const call = http.expectOne('http://api.test/admin/quota-requests/req-1/approve');
    expect(call.request.method).toBe('POST');
    expect(call.request.body).toMatchObject({ amount: 20, validUntil: '2026-11-30', fundingMode: 'central', standing: false });
    call.flush({ request: { requestId: 'req-1' }, extension: { extensionId: 'ext-1' } });
    const result = await approving;
    expect(result.extension.extensionId).toBe('ext-1');
    http.expectOne('http://api.test/admin/quota-requests/summary').flush({ pending: 0, infoRequested: 0 });
  });

  it('lists active extensions for one user', async () => {
    const listing = service.listExtensions({ userId: 'u 1', active: true });
    const call = http.expectOne(r => r.url === 'http://api.test/admin/quota-extensions');
    expect(call.request.params.get('userId')).toBe('u 1');
    expect(call.request.params.get('active')).toBe('true');
    call.flush([]);
    expect(await listing).toEqual([]);
  });
});

describe('quotaAdminErrorMessage', () => {
  it('uses the API detail, the first validation message, or the fallback', () => {
    const detail = new HttpErrorResponse({ status: 403, error: { detail: 'You cannot decide your own quota request' } });
    expect(quotaAdminErrorMessage(detail, 'x')).toBe('You cannot decide your own quota request');

    const validation = new HttpErrorResponse({ status: 422, error: { detail: [{ msg: 'Value error, bad amount' }] } });
    expect(quotaAdminErrorMessage(validation, 'x')).toBe('bad amount');

    expect(quotaAdminErrorMessage(new HttpErrorResponse({ status: 500 }), 'fallback')).toBe('fallback');
    expect(quotaAdminErrorMessage(new Error('boom'), 'fallback')).toBe('fallback');
  });
});
