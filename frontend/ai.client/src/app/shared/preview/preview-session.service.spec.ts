import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { effect, signal } from '@angular/core';
import { PreviewSessionService } from './preview-session.service';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { ChatHttpService } from '../../session/services/chat/chat-http.service';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { StreamParserService } from '../../session/services/chat/stream-parser.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { PREVIEW_SESSION_PREFIX } from '../constants/session.constants';

describe('PreviewSessionService', () => {
  let service: PreviewSessionService;
  let chatRequest: { submitPreviewRequest: ReturnType<typeof vi.fn> };
  let chatHttp: { cancelChatRequest: ReturnType<typeof vi.fn> };
  let messageMap: {
    getMessagesForSession: ReturnType<typeof vi.fn>;
    clearSession: ReturnType<typeof vi.fn>;
  };
  // A signal, not a plain Set: `isLoading` is a computed, so the backing state
  // has to be reactive or the test would assert against a stale read and pass
  // for the wrong reason.
  let loadingSessions: ReturnType<typeof signal<ReadonlySet<string>>>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    loadingSessions = signal<ReadonlySet<string>>(new Set());

    chatRequest = { submitPreviewRequest: vi.fn().mockResolvedValue(undefined) };
    chatHttp = { cancelChatRequest: vi.fn() };
    messageMap = {
      // A fresh signal per session id, so a test can tell which session the
      // service is projecting.
      getMessagesForSession: vi.fn((id: string) => signal([{ id: `msg-${id}` }])),
      clearSession: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        PreviewSessionService,
        { provide: ChatRequestService, useValue: chatRequest },
        { provide: ChatHttpService, useValue: chatHttp },
        {
          provide: ChatStateService,
          useValue: { isSessionLoading: (id: string) => loadingSessions().has(id) },
        },
        {
          provide: StreamParserService,
          useValue: {
            streamingMessageIdFor: () => signal<string | null>(null),
            errorFor: () => signal<string | null>(null),
          },
        },
        { provide: MessageMapService, useValue: messageMap },
      ],
    });

    service = TestBed.inject(PreviewSessionService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /** The backend keys persistence-skipping, metadata-skipping and the
   *  single-flight lease exemption off this prefix. */
  it('mints a session id the backend recognises as a preview', () => {
    expect(service.sessionId().startsWith(PREVIEW_SESSION_PREFIX)).toBe(true);
  });

  it('sends through the shared chat request path, keyed to its own session', async () => {
    await service.send('ast-001', 'hello', { fileUploadIds: ['up-1'] });

    expect(chatRequest.submitPreviewRequest).toHaveBeenCalledWith({
      sessionId: service.sessionId(),
      agentId: 'ast-001',
      message: 'hello',
      fileUploadIds: ['up-1'],
      reviewPreview: undefined,
    });
  });

  it('forwards the reviewer flag when the caller is a reviewer', async () => {
    await service.send('ast-001', 'hello', { reviewPreview: true });

    expect(chatRequest.submitPreviewRequest).toHaveBeenCalledWith(
      expect.objectContaining({ reviewPreview: true }),
    );
  });

  it('cancels its own session only', () => {
    const id = service.sessionId();
    service.cancel();
    expect(chatHttp.cancelChatRequest).toHaveBeenCalledWith(id);
  });

  it('projects loading state for its own session, not for another one', () => {
    expect(service.isLoading()).toBe(false);

    // Some other conversation streaming must not light up this pane.
    loadingSessions.set(new Set(['some-other-session']));
    expect(service.isLoading()).toBe(false);

    loadingSessions.set(new Set([service.sessionId()]));
    expect(service.isLoading()).toBe(true);
  });

  describe('reset', () => {
    /**
     * ⚠️ The NEW session id is the point, not a side effect.
     *
     * Aborting the SSE does not stop the agent server-side — a client abort does
     * not propagate through the AgentCore Runtime data plane — so the previous
     * run may still be alive. Reusing the id let the next message collide with
     * it, which surfaced to users as "Agent is already processing a request.
     * Concurrent invocations are not supported." A new id cannot collide, and
     * the abandoned run has nothing to corrupt because preview sessions persist
     * nothing.
     */
    it('starts a new session rather than reusing the one that may still be streaming', () => {
      const before = service.sessionId();

      service.reset();

      const after = service.sessionId();
      expect(after).not.toBe(before);
      expect(after.startsWith(PREVIEW_SESSION_PREFIX)).toBe(true);
    });

    it('cancels the in-flight turn and drops the old transcript', () => {
      const before = service.sessionId();

      service.reset();

      expect(chatHttp.cancelChatRequest).toHaveBeenCalledWith(before);
      expect(messageMap.clearSession).toHaveBeenCalledWith(before);
    });

    it('projects the new session after a reset', () => {
      service.reset();

      expect(service.messages()).toEqual([{ id: `msg-${service.sessionId()}` }]);
    });

    /**
     * ⚠️ THE regression test for a self-inflicted page freeze.
     *
     * Both preview surfaces call `reset()` from an `effect` that watches the
     * agent id. `reset()` reads the current session id and then writes a new
     * one — if those reads are tracked, the effect registers a dependency on
     * the signal it just wrote and re-triggers itself forever.
     *
     * It fails silently and catastrophically: no error, no stack. The main
     * thread pegs, so the page paints its loading skeleton, the HTTP responses
     * arrive but are never processed, and the whole tab presents as "the API
     * calls never resolved." That is exactly how it showed up in the browser.
     *
     * Asserting on a call count rather than waiting for a hang: a runaway
     * effect flushes many times per `detectChanges`, so one write == one reset.
     */
    it('does not re-trigger an effect that calls it (no read-write cycle)', () => {
      const agentId = signal('ast-001');
      let resets = 0;

      TestBed.runInInjectionContext(() => {
        effect(() => {
          if (agentId()) {
            resets++;
            service.reset();
          }
        });
      });

      TestBed.tick();
      expect(resets).toBe(1);

      // A genuine input change must still re-run it exactly once more.
      agentId.set('ast-002');
      TestBed.tick();
      expect(resets).toBe(2);
    });

    it('sends subsequent turns on the new session', async () => {
      service.reset();
      const after = service.sessionId();

      await service.send('ast-001', 'hello');

      expect(chatRequest.submitPreviewRequest).toHaveBeenCalledWith(
        expect.objectContaining({ sessionId: after }),
      );
    });
  });
});
