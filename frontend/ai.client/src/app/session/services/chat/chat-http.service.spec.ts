import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ChatHttpService } from './chat-http.service';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { SessionService } from '../session/session.service';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { ErrorService } from '../../../services/error/error.service';

describe('ChatHttpService', () => {
  let service: ChatHttpService;
  let httpMock: HttpTestingController;
  let chatStateService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ChatHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        // Phase 6c: chat-http now reads CSRF from the BFF SessionService
        // and lets cookie auth ride along with the request rather than
        // attaching a Bearer manually.
        { provide: BffSessionService, useValue: { csrfHeaders: vi.fn().mockReturnValue({}), handleUnauthorized: vi.fn() } },
        { provide: SessionService, useValue: { currentSession: signal({ sessionId: 's1' }), updateSessionTitleInCache: vi.fn() } },
        { provide: StreamParserService, useValue: { getCurrentStreamId: vi.fn().mockReturnValue('stream-1'), parseEventSourceMessage: vi.fn() } },
        { provide: ChatStateService, useValue: { abortRequest: vi.fn(), setChatLoading: vi.fn(), setLastTurnInterrupted: vi.fn(), createAbortController: vi.fn().mockReturnValue(new AbortController()) } },
        { provide: MessageMapService, useValue: { endStreaming: vi.fn() } },
        { provide: ErrorService, useValue: { handleHttpError: vi.fn() } },
      ],
    });
    service = TestBed.inject(ChatHttpService);
    httpMock = TestBed.inject(HttpTestingController);
    chatStateService = TestBed.inject(ChatStateService);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should generate title', async () => {
    const promise = service.generateTitle('s1', 'Hello');
    const req = httpMock.expectOne(r => r.url.includes('/chat/generate-title'));
    expect(req.request.method).toBe('POST');
    req.flush({ title: 'Generated Title', session_id: 's1' });
    const result = await promise;
    expect(result.title).toBe('Generated Title');
  });

  it('should cancel only the target session and tear down its streaming state', () => {
    const messageMap = TestBed.inject(MessageMapService) as any;
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    service.cancelChatRequest('s1');

    expect(chatStateService.abortRequest).toHaveBeenCalledWith('s1');
    expect(chatStateService.setChatLoading).toHaveBeenCalledWith('s1', false);
    expect(messageMap.endStreaming).toHaveBeenCalledWith('s1');
    vi.restoreAllMocks();
  });

  it('fires a keepalive user_stopped signal and reflects the interruption locally on cancel', () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(null, { status: 204 }));

    service.cancelChatRequest('s1');

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe('http://localhost:8000/sessions/s1/interrupt');
    expect(init?.method).toBe('POST');
    expect(init?.keepalive).toBe(true);
    expect(init?.credentials).toBe('include');
    expect(JSON.parse(init?.body as string)).toEqual({ reason: 'user_stopped' });

    // The chip reflects locally without waiting for a reload.
    expect(chatStateService.setLastTurnInterrupted).toHaveBeenCalledWith('s1', true, 'user_stopped');
    vi.restoreAllMocks();
  });

  it('a failed stop signal never blocks the Stop teardown', () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));

    expect(() => service.cancelChatRequest('s1')).not.toThrow();
    expect(chatStateService.abortRequest).toHaveBeenCalledWith('s1');
    vi.restoreAllMocks();
  });
});
