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
        { provide: SessionService, useValue: { currentSession: signal({ sessionId: 's1' }), updateSessionTitleInCache: vi.fn(), getSessionMetadata: vi.fn().mockResolvedValue({}) } },
        { provide: StreamParserService, useValue: { getCurrentStreamId: vi.fn().mockReturnValue('stream-1'), parseEventSourceMessage: vi.fn() } },
        { provide: ChatStateService, useValue: { abortRequest: vi.fn(), setChatLoading: vi.fn(), setLastTurnInterrupted: vi.fn(), seedSessionAggregates: vi.fn(), createAbortController: vi.fn().mockReturnValue(new AbortController()), streamingSessionIds: vi.fn().mockReturnValue([]) } },
        { provide: MessageMapService, useValue: { endStreaming: vi.fn() } },
        { provide: ErrorService, useValue: { handleHttpError: vi.fn(), addError: vi.fn() } },
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

  it('re-seeds the session cost badge after a stop once the backend persisted the interrupted turn', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    const sessionSvc = TestBed.inject(SessionService) as any;
    sessionSvc.getSessionMetadata = vi
      .fn()
      .mockResolvedValue({ totalCost: 0.0123, lastContextTokens: 4200, contextWindow: 200000 });

    service.cancelChatRequest('s1');

    // The refresh is delayed to let the teardown write land, then awaits the fetch.
    await vi.runAllTimersAsync();

    expect(sessionSvc.getSessionMetadata).toHaveBeenCalledWith('s1');
    expect(chatStateService.seedSessionAggregates).toHaveBeenCalledWith('s1', {
      totalCost: 0.0123,
      lastContextTokens: 4200,
      contextWindow: 200000,
    });
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('retries the cost refresh once when the interrupted-turn write has not landed yet', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    const sessionSvc = TestBed.inject(SessionService) as any;
    // First read races the teardown write (empty aggregates); second succeeds.
    sessionSvc.getSessionMetadata = vi
      .fn()
      .mockResolvedValueOnce({})
      .mockResolvedValue({ totalCost: 0.02, lastContextTokens: 100, contextWindow: 200000 });

    service.cancelChatRequest('s1');
    await vi.runAllTimersAsync();

    expect(sessionSvc.getSessionMetadata).toHaveBeenCalledTimes(2);
    expect(chatStateService.seedSessionAggregates).toHaveBeenCalledWith('s1', {
      totalCost: 0.02,
      lastContextTokens: 100,
      contextWindow: 200000,
    });
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('surfaces a soft "Already responding" notice (not a hard error) on a 409 single-flight rejection', async () => {
    // The inference-api single-flight guard rejects a duplicate turn while the
    // prior one is still streaming server-side; the BFF relays it as 409.
    const body = JSON.stringify({
      detail: 'A response is already streaming for this conversation. Wait for it to finish before sending another message.',
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body, { status: 409, headers: { 'content-type': 'application/json' } }),
    );
    const errorSvc = TestBed.inject(ErrorService) as any;

    await expect(
      service.sendChatRequest({ session_id: 's1', message: 'hi' }),
    ).rejects.toMatchObject({ name: 'AlreadyStreamingError' });

    // Gentle, dismissible notice carrying the server's explanation — NOT the
    // "Chat Request Failed" / network-error paths.
    expect(errorSvc.addError).toHaveBeenCalledTimes(1);
    const [title, message] = errorSvc.addError.mock.calls[0];
    expect(title).toBe('Already responding');
    expect(message).toContain('already streaming');
    // Loading is cleared so the user can retry once the prior turn finishes.
    expect(chatStateService.setChatLoading).toHaveBeenCalledWith('s1', false);
    vi.restoreAllMocks();
  });

  it('unwraps a double-encoded 409 body from the BFF proxy', async () => {
    // app-api relays inference-api's body verbatim inside its own `detail`,
    // so the payload can be `{"detail":"{\"detail\":\"…\"}"}`.
    const inner = JSON.stringify({ detail: 'This conversation is busy generating a response.' });
    const body = JSON.stringify({ detail: inner });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body, { status: 409, headers: { 'content-type': 'application/json' } }),
    );
    const errorSvc = TestBed.inject(ErrorService) as any;

    await expect(
      service.sendChatRequest({ session_id: 's1', message: 'hi' }),
    ).rejects.toMatchObject({ name: 'AlreadyStreamingError' });

    const [, message] = errorSvc.addError.mock.calls[0];
    expect(message).toBe('This conversation is busy generating a response.');
    vi.restoreAllMocks();
  });

  describe('page-departure attribution', () => {
    // A refresh / tab close / navigation is the one interruption cause only
    // the browser witnesses. Unattested it lands server-side as
    // `connection_lost` — indistinguishable from a dropped socket or a
    // platform-side idle timeout — which is what made the remaining drops
    // undiagnosable in the first place.

    interface InterruptCall {
      url: string;
      body: { reason: string };
      keepalive: boolean | undefined;
    }

    function interruptCalls(fetchSpy: any): InterruptCall[] {
      return fetchSpy.mock.calls
        .filter(([url]: [string]) => String(url).includes('/interrupt'))
        .map(([url, init]: [string, RequestInit]) => ({
          url: String(url),
          body: JSON.parse(String(init.body)),
          keepalive: init.keepalive,
        }));
    }

    it('signals navigated_away for each streaming session on pagehide', () => {
      chatStateService.streamingSessionIds.mockReturnValue(['s1', 's2']);
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

      window.dispatchEvent(new Event('pagehide'));

      const calls = interruptCalls(fetchSpy);
      expect(calls).toHaveLength(2);
      expect(calls.map((c: InterruptCall) => c.body.reason)).toEqual(['navigated_away', 'navigated_away']);
      expect(calls[0].url).toContain('/sessions/s1/interrupt');
      expect(calls[1].url).toContain('/sessions/s2/interrupt');
      // `keepalive` is what lets the request outlive the page being torn down.
      expect(calls.every((c: InterruptCall) => c.keepalive)).toBe(true);
      vi.restoreAllMocks();
    });

    it('signals nothing when no turn is in flight', () => {
      chatStateService.streamingSessionIds.mockReturnValue([]);
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

      window.dispatchEvent(new Event('pagehide'));

      expect(interruptCalls(fetchSpy)).toHaveLength(0);
      vi.restoreAllMocks();
    });

    it('does not abort the stream — attribution must not intervene', () => {
      // Aborting on page-hide would kill turns for a bfcache navigation the
      // user may come back from, and the server turn is meant to keep running
      // so a reload can offer to continue it.
      chatStateService.streamingSessionIds.mockReturnValue(['s1']);
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

      window.dispatchEvent(new Event('pagehide'));

      expect(chatStateService.abortRequest).not.toHaveBeenCalled();
      vi.restoreAllMocks();
    });
  });
});
