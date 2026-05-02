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
        { provide: BffSessionService, useValue: { csrfHeaders: vi.fn().mockReturnValue({}) } },
        { provide: SessionService, useValue: { currentSession: signal({ sessionId: 's1' }), updateSessionTitleInCache: vi.fn() } },
        { provide: StreamParserService, useValue: {} },
        { provide: ChatStateService, useValue: { isStreaming: signal(false), streamingSessionId: signal(null), abortCurrentRequest: vi.fn(), setChatLoading: vi.fn(), resetState: vi.fn(), getAbortController: vi.fn().mockReturnValue(new AbortController()) } },
        { provide: MessageMapService, useValue: {} },
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

  it('should cancel chat request', () => {
    service.cancelChatRequest();
    expect(chatStateService.abortCurrentRequest).toHaveBeenCalled();
    expect(chatStateService.setChatLoading).toHaveBeenCalledWith(false);
    expect(chatStateService.resetState).toHaveBeenCalled();
  });
});
