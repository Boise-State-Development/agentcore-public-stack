import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { RunApiService } from './run-api.service';
import { ConfigService } from '../../services/config.service';
import { RunNowResponse } from '../models/schedule.model';

const BASE = 'http://localhost:8000/runs';

function stubRunResponse(overrides: Partial<RunNowResponse> = {}): RunNowResponse {
  return {
    runId: 'run-1',
    sessionId: 'sess-1',
    status: 'completed',
    finalMessage: 'done',
    stopReason: null,
    error: null,
    title: null,
    ...overrides,
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
    const result = stubRunResponse();
    const promise = firstValueFrom(service.runNow({ prompt: 'Do the thing', title: 'Test' }));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/now`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ prompt: 'Do the thing', title: 'Test' });
      req.flush(result);
    });

    expect(await promise).toEqual(result);
  });
});
