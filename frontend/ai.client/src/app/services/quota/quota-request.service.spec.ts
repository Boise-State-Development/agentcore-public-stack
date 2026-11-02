import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { QuotaRequestFailure, QuotaRequestService } from './quota-request.service';
import { QuotaStatusService } from './quota-status.service';
import { QuotaWarningService } from './quota-warning.service';
import { ConfigService } from '../config.service';

describe('QuotaRequestService', () => {
  let http: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>; patch: ReturnType<typeof vi.fn> };
  const reload = vi.fn();
  const clearQuotaExceeded = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    http = { get: vi.fn().mockReturnValue(of([])), post: vi.fn(), patch: vi.fn() };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: HttpClient, useValue: http },
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
        { provide: QuotaStatusService, useValue: { reload } },
        { provide: QuotaWarningService, useValue: { clearQuotaExceeded } },
      ],
    });
  });
  afterEach(() => TestBed.resetTestingModule());

  it('submits to /quota/requests and re-reads the status', async () => {
    http.post.mockReturnValue(of({ requestId: 'r1' }));
    const service = TestBed.inject(QuotaRequestService);
    const body = { kind: 'one_time' as const, requestedAmount: 25, requestedUntil: '2026-11-30', justification: 'x'.repeat(20) };
    await service.submit(body);
    expect(http.post).toHaveBeenCalledWith('http://api.test/quota/requests', body, expect.anything());
    expect(reload).toHaveBeenCalled();
  });

  it('turns a 409 into a failure carrying the open request id', async () => {
    http.post.mockReturnValue(throwError(() => new HttpErrorResponse({
      status: 409, error: { detail: 'You already have an open quota request', openRequestId: 'r9' },
    })));
    const service = TestBed.inject(QuotaRequestService);
    const failure = await service.submit({ kind: 'one_time', requestedAmount: 1, requestedUntil: '2026-11-30', justification: '' }).catch(e => e);
    expect(failure).toBeInstanceOf(QuotaRequestFailure);
    expect(failure.message).toBe('You already have an open quota request');
    expect(failure.openRequestId).toBe('r9');
  });

  it('reads a validation error’s first message', async () => {
    http.patch.mockReturnValue(throwError(() => new HttpErrorResponse({
      status: 422, error: { detail: [{ msg: 'Value error, Fund is required' }] },
    })));
    const failure = await TestBed.inject(QuotaRequestService).addFundingSource('r1', { fields: {} }).catch(e => e);
    expect(failure.message).toBe('Fund is required');
  });

  it('withdraws with a PATCH', async () => {
    http.patch.mockReturnValue(of({}));
    await TestBed.inject(QuotaRequestService).withdraw('r1');
    expect(http.patch).toHaveBeenCalledWith('http://api.test/quota/requests/r1', { withdraw: true }, expect.anything());
  });

  it('lifts the blocked state on a decision or a grant, and re-reads the status for any quota notice', () => {
    const service = TestBed.inject(QuotaRequestService);
    service.onQuotaNotification('quota_request_info_requested');
    expect(clearQuotaExceeded).not.toHaveBeenCalled();
    expect(reload).toHaveBeenCalledTimes(1);
    service.onQuotaNotification('quota_request_decided');
    expect(clearQuotaExceeded).toHaveBeenCalledTimes(1);
  });

  it('caches the policy, and retries after a failed read', async () => {
    http.get.mockReturnValueOnce(throwError(() => new HttpErrorResponse({ status: 500 })));
    const service = TestBed.inject(QuotaRequestService);
    await expect(service.policy()).rejects.toBeTruthy();
    http.get.mockReturnValue(of({ amountPresets: ['10'] }));
    await service.policy();
    await service.policy();
    expect(http.get).toHaveBeenCalledTimes(2);
  });
});
