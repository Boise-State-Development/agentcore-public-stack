import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { FETCH_EVENT_SOURCE, PreviewChatService } from './preview-chat.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';

describe('PreviewChatService', () => {
  let service: PreviewChatService;
  let bffSession: any;
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();

    // Inject the SSE client through Angular DI rather than mocking the
    // `@microsoft/fetch-event-source` module via vi.mock. The vi.mock
    // approach raced with sibling specs that transitively import this
    // service in the Angular vitest builder's shared worker pool, causing
    // the production code to call a different vi.fn() instance than the
    // one this spec captured.
    mockFetch = vi.fn().mockResolvedValue(undefined);

    // Phase 6c: preview chat now goes through the BFF chat proxy with
    // cookie auth, so the only auth surface the service touches is the
    // CSRF helper on the BFF SessionService.
    const bffSessionMock = {
      csrfHeaders: vi.fn().mockReturnValue({}),
    };

    const configServiceMock = {
      appApiUrl: signal('http://localhost:8000'),
    };

    TestBed.configureTestingModule({
      providers: [
        PreviewChatService,
        { provide: FETCH_EVENT_SOURCE, useValue: mockFetch },
        { provide: BffSessionService, useValue: bffSessionMock },
        { provide: ConfigService, useValue: configServiceMock },
      ],
    });

    service = TestBed.inject(PreviewChatService);
    bffSession = TestBed.inject(BffSessionService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should have initial state', () => {
    expect(service.messages()).toEqual([]);
    expect(service.isLoading()).toBe(false);
    expect(service.hasMessages()).toBe(false);
    expect(service.sessionId()).toMatch(/^preview-/);
  });

  it('should send message', async () => {
    await service.sendMessage('Hello', 'assistant-1', 'Test instructions');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(service.messages()).toHaveLength(2);
    expect(service.messages()[0].role).toBe('user');
    expect(service.messages()[1].role).toBe('assistant');
  });

  it('should handle empty message', async () => {
    await service.sendMessage('', 'assistant-1');

    expect(service.messages()).toHaveLength(0);
    expect(service.isLoading()).toBe(false);
  });

  it('should cancel request', () => {
    service.cancelRequest();

    expect(service.isLoading()).toBe(false);
    expect(service.streamingMessageId()).toBeNull();
  });

  it('should clear messages', () => {
    // Add a message first
    service['messagesSignal'].set([{
      id: 'test',
      role: 'user',
      content: [{ type: 'text', text: 'test' }],
      createdAt: new Date().toISOString(),
    }]);

    service.clearMessages();

    expect(service.messages()).toEqual([]);
    expect(service.error()).toBeNull();
  });

  it('should reset with new session ID', () => {
    const oldSessionId = service.sessionId();

    service.reset();

    expect(service.messages()).toEqual([]);
    expect(service.sessionId()).not.toBe(oldSessionId);
    expect(service.sessionId()).toMatch(/^preview-/);
  });

  it('attaches the CSRF header from the BFF SessionService when present', async () => {
    bffSession.csrfHeaders.mockReturnValue({ 'X-CSRF-Token': 'tok-xyz' });

    await service.sendMessage('Hello', 'assistant-1');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const init = mockFetch.mock.calls[0][1]!;
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('tok-xyz');
    expect(init.credentials).toBe('include');
  });
});
