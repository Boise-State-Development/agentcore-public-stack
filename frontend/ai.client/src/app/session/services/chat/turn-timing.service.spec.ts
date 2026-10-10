import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { DOCUMENT } from '@angular/common';
import { PLATFORM_ID } from '@angular/core';
import { TurnTimingService } from './turn-timing.service';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';

const API = 'http://api.test';
const URL = `${API}/telemetry/turn-timing`;

describe('TurnTimingService', () => {
  let httpMock: HttpTestingController;

  function setup(options: { enabled?: boolean; visibility?: DocumentVisibilityState } = {}) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: FEATURES, useValue: { clientTurnTiming: options.enabled ?? true } },
        { provide: DOCUMENT, useValue: { visibilityState: options.visibility ?? 'visible' } },
        { provide: PLATFORM_ID, useValue: 'browser' },
      ],
    });
    httpMock = TestBed.inject(HttpTestingController);
    return TestBed.inject(TurnTimingService);
  }

  afterEach(() => {
    httpMock.verify();
    vi.restoreAllMocks();
  });

  it('reports send-to-first-token once, with the turn context, toasts suppressed', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000);
    const service = setup();
    service.start('conv-1', { turnKind: 'first', warmAgeMs: 6100 });
    service.describe('conv-1', { modelId: 'us.anthropic.claude-haiku-4-5', hasAgent: true, hasFiles: false });

    now.mockReturnValue(3360.4);
    service.firstToken('conv-1');
    const req = httpMock.expectOne(URL);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      firstTokenMs: 2360,
      turnKind: 'first',
      warmAgeMs: 6100,
      modelId: 'us.anthropic.claude-haiku-4-5',
      hasAgent: true,
      hasFiles: false,
      visible: true,
    });
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush(null, { status: 204, statusText: 'No Content' });

    // Later deltas of the same turn report nothing.
    service.firstToken('conv-1');
    httpMock.expectNone(URL);
  });

  it('sends no identity: no session id, user id or message content', () => {
    const service = setup();
    service.start('conv-secret-id', { turnKind: 'followup', warmAgeMs: null });
    service.firstToken('conv-secret-id');
    const req = httpMock.expectOne(URL);
    expect(JSON.stringify(req.request.body)).not.toContain('conv-secret-id');
    req.flush(null, { status: 204, statusText: 'No Content' });
  });

  it('a turn abandoned before its first token reports nothing', () => {
    const service = setup();
    service.start('conv-1', { turnKind: 'first', warmAgeMs: null });
    service.abandon('conv-1');
    service.firstToken('conv-1');
    httpMock.expectNone(URL);
  });

  it('a delta with no send behind it (resume, Continue) reports nothing', () => {
    const service = setup();
    service.firstToken('conv-1');
    httpMock.expectNone(URL);
  });

  it('records whether the tab was visible when the token arrived', () => {
    const service = setup({ visibility: 'hidden' });
    service.start('conv-1', { turnKind: 'followup', warmAgeMs: null });
    service.firstToken('conv-1');
    const req = httpMock.expectOne(URL);
    expect(req.request.body.visible).toBe(false);
    req.flush(null, { status: 204, statusText: 'No Content' });
  });

  it('swallows a failed report', () => {
    const service = setup();
    service.start('conv-1', { turnKind: 'first', warmAgeMs: null });
    expect(() => service.firstToken('conv-1')).not.toThrow();
    httpMock.expectOne(URL).flush({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
  });

  it('measures nothing while the flag is off', () => {
    const service = setup({ enabled: false });
    service.start('conv-1', { turnKind: 'first', warmAgeMs: null });
    service.firstToken('conv-1');
    httpMock.expectNone(URL);
  });
});
