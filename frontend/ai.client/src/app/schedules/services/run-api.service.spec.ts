import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { RunApiService } from './run-api.service';
import { ConfigService } from '../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { RunStartedResponse, RunStatusResponse } from '../models/schedule.model';

const BASE = 'http://localhost:8000/runs';

function stubStarted(): RunStartedResponse {
  return { runId: 'run-0123456789ab', sessionId: 'headless-1', status: 'started' };
}

function stubStatus(): RunStatusResponse {
  return {
    runId: 'run-0123456789ab',
    sessionId: 'headless-1',
    status: 'completed',
    title: 'T',
    error: null,
    startedAt: '2026-11-19T00:00:00Z',
    finishedAt: '2026-11-19T00:01:10Z',
  };
}

describe('RunApiService', () => {
  let service: RunApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        RunApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(RunApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('POSTs to /runs/now with the prompt payload', async () => {
    const result = stubStarted();
    const promise = firstValueFrom(service.runNow({ prompt: 'Do the thing', title: 'Test' }));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/now`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ prompt: 'Do the thing', title: 'Test' });
      req.flush(result);
    });

    expect(await promise).toEqual(result);
  });

  it('GETs /runs/{runId} without the global error toast', async () => {
    const result = stubStatus();
    const promise = firstValueFrom(service.getRun('run-0123456789ab'));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/run-0123456789ab`);
      expect(req.request.method).toBe('GET');
      expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
      req.flush(result);
    });

    expect(await promise).toEqual(result);
  });
});
