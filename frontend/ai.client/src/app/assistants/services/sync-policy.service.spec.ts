import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { SyncPolicyService, SyncPolicyError } from './sync-policy.service';
import { ConfigService } from '../../services/config.service';
import { SyncPolicy } from '../models/sync-policy.model';

const BASE = 'http://localhost:8000/assistants/assistant1/sync-policies';

function stubPolicy(overrides: Partial<SyncPolicy> = {}): SyncPolicy {
  return {
    policyId: 'syn-abc123def456',
    assistantId: 'assistant1',
    sourceType: 'drive_file',
    sourceRef: 'doc-1',
    interval: 'daily',
    state: 'active',
    stateReason: null,
    nextSyncAt: '2026-07-04T00:00:00Z',
    lastSyncAt: null,
    lastResult: null,
    createdAt: '2026-07-03T00:00:00Z',
    updatedAt: '2026-07-03T00:00:00Z',
    ...overrides,
  };
}

describe('SyncPolicyService', () => {
  let service: SyncPolicyService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SyncPolicyService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(SyncPolicyService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should list policies', async () => {
    const policies = [stubPolicy()];

    const promise = service.listPolicies('assistant1');

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('GET');
      req.flush({ policies });
    });

    expect(await promise).toEqual(policies);
  });

  it('should create a policy', async () => {
    const policy = stubPolicy();

    const promise = service.createPolicy('assistant1', {
      sourceType: 'drive_file',
      sourceRef: 'doc-1',
      interval: 'daily',
    });

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({
        sourceType: 'drive_file',
        sourceRef: 'doc-1',
        interval: 'daily',
      });
      req.flush(policy, { status: 201, statusText: 'Created' });
    });

    expect(await promise).toEqual(policy);
  });

  it('should update a policy interval', async () => {
    const policy = stubPolicy({ interval: 'weekly' });

    const promise = service.updatePolicy('assistant1', 'syn-abc123def456', {
      interval: 'weekly',
    });

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/syn-abc123def456`);
      expect(req.request.method).toBe('PATCH');
      expect(req.request.body).toEqual({ interval: 'weekly' });
      req.flush(policy);
    });

    expect(await promise).toEqual(policy);
  });

  it('should delete a policy', async () => {
    const promise = service.deletePolicy('assistant1', 'syn-abc123def456');

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/syn-abc123def456`);
      expect(req.request.method).toBe('DELETE');
      req.flush(null, { status: 204, statusText: 'No Content' });
    });

    await promise;
  });

  it('should request run-now', async () => {
    const policy = stubPolicy({ nextSyncAt: '2026-07-03T00:00:01Z' });

    const promise = service.runNow('assistant1', 'syn-abc123def456');

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/syn-abc123def456/run-now`);
      expect(req.request.method).toBe('POST');
      req.flush(policy, { status: 202, statusText: 'Accepted' });
    });

    expect(await promise).toEqual(policy);
  });

  it('should surface the server detail and status on a run-now cooldown (429)', async () => {
    const promise = service.runNow('assistant1', 'syn-abc123def456');

    await vi.waitFor(() => {
      httpMock
        .expectOne(`${BASE}/syn-abc123def456/run-now`)
        .flush(
          { detail: 'A manual sync was already requested recently; try again in a few minutes' },
          { status: 429, statusText: 'Too Many Requests' },
        );
    });

    const error = await promise.then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(SyncPolicyError);
    expect((error as SyncPolicyError).status).toBe(429);
    expect((error as SyncPolicyError).code).toBe('HTTP_429');
    expect((error as SyncPolicyError).message).toContain('try again in a few minutes');
  });

  it('should surface the reauth-resume conflict (409) from PATCH', async () => {
    const promise = service.updatePolicy('assistant1', 'syn-abc123def456', { state: 'active' });

    await vi.waitFor(() => {
      httpMock
        .expectOne(`${BASE}/syn-abc123def456`)
        .flush(
          { detail: 'Reconnect the content source to resume syncing' },
          { status: 409, statusText: 'Conflict' },
        );
    });

    const error = await promise.then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(SyncPolicyError);
    expect((error as SyncPolicyError).status).toBe(409);
    expect((error as SyncPolicyError).message).toBe(
      'Reconnect the content source to resume syncing',
    );
  });

  it('should fall back to a generic message on non-HTTP failures', async () => {
    const promise = service.listPolicies('assistant1');

    await vi.waitFor(() => {
      httpMock.expectOne(BASE).error(new ProgressEvent('error'));
    });

    const error = await promise.then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(SyncPolicyError);
  });
});
